"""One replacement trial: provision, run, inject, replace, settle, observe."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path

from cdk_seed.cfn_deploy import deploy
from collections import Counter

from botocore.exceptions import ClientError

from harness import (
    AppendService,
    HarnessFailure,
    PARAMS,
    ReadGate,
    WorkerProcess,
    client,
    detail,
    read_output,
    retryable,
)

SETUP_TIMEOUT = 150


class Observer:
    """Independent reader that records when the in-flight message is delivered."""

    def __init__(self, kinesis, stream, gate, limit, target_id):
        self._kinesis = kinesis
        self._stream = stream
        self._gate = gate
        self._limit = limit
        self._target_id = target_id
        self.first_seen = None
        self.target_sequences = []
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._started = time.monotonic()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        iterator = retryable(
            lambda: self._kinesis.get_shard_iterator(
                StreamName=self._stream,
                ShardId="shardId-000000000000",
                ShardIteratorType="TRIM_HORIZON",
            ),
            safe=True,
        )["ShardIterator"]
        while not self._stop.is_set():
            try:
                self._gate.wait()
                response = retryable(
                    lambda: self._kinesis.get_records(
                        ShardIterator=iterator, Limit=self._limit
                    ),
                    safe=True,
                )
            except ClientError:
                return
            iterator = response.get("NextShardIterator", iterator)
            for record in response.get("Records", []):
                payload = json.loads(record["Data"].decode("utf-8"))
                if payload.get("messageId") == self._target_id:
                    if self.first_seen is None:
                        self.first_seen = time.monotonic() - self._started
                    self.target_sequences.append(record["SequenceNumber"])

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)


class Trial:
    def __init__(self, variant, endpoint):
        self.variant = variant
        self.endpoint = endpoint
        self.params = dict(PARAMS)
        self.run_id = uuid.uuid4().hex[:12]
        self.partition_id = "part-%s" % self.run_id[:8]
        self.input_stream = "%s-in" % self.run_id
        self.output_stream = "%s-out" % self.run_id
        self.table = "%s-state" % self.run_id
        self.pk = "RUN#%s#PART#%s" % (self.run_id, self.partition_id)
        self.workroot = tempfile.mkdtemp(prefix="trial-%s-" % self.variant)
        self.result = {
            "variant": variant,
            "run_id": self.run_id,
            "ok": False,
            "phase": "start",
            "reason": "",
            "fixture": False,
            "records": [],
            "counts": {},
            "expected": {},
            "target_id": None,
            "target_first_seen": None,
            "recovery_deadline": self.params["recovery_deadline_seconds"],
            "completion_deadline": self.params["completion_deadline_seconds"],
            "final_checkpoint": None,
            "final_position": None,
            "rows": {},
            "quiesced": False,
            "appended_ids": [],
            "service_sequences": [],
            "events": [],
            "stderr": [],
            "timeline": [],
        }
        self.workers = []
        self.service = None
        self.observer = None
        self._input_sequence = 0

    # -- helpers ----------------------------------------------------------

    def _mark(self, message):
        stamp = time.strftime("%H:%M:%S", time.localtime())
        self.result["timeline"].append("%s %s" % (stamp, message))
        detail("trial %s (%s): %s" % (self.run_id, self.variant, message))

    def _put_input(self, kinesis, message_id, payload):
        """Submit one input record. The trial is the only writer this stream
        has and submits sequentially, so arrival order is input order."""
        self._input_sequence += 1
        retryable(
            lambda: kinesis.put_record(
                StreamName=self.input_stream,
                Data=json.dumps(
                    {
                        "kind": "DATA",
                        "input_sequence": str(self._input_sequence),
                        "messageId": message_id,
                        "payload": payload,
                    }
                ).encode("utf-8"),
                PartitionKey=self.partition_id,
            )
        )
        return self._input_sequence

    def _control(self, dynamodb):
        response = retryable(
            lambda: dynamodb.get_item(
                TableName=self.table,
                Key={"pk": {"S": self.pk}, "sk": {"S": "CONTROL"}},
                ConsistentRead=True,
            ),
            safe=True,
        )
        return response.get("Item")

    @staticmethod
    def _scalar(item, name, default=None):
        value = item.get(name) if item else None
        if not value:
            return default
        for kind in ("S", "N"):
            if kind in value:
                return value[kind]
        return default

    def _wait_checkpoint(self, dynamodb, target, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            control = self._control(dynamodb)
            checkpoint = self._scalar(control, "input_checkpoint", "START")
            if checkpoint == str(target):
                return True
            time.sleep(0.25)
        return False

    def _write_config(self, name, epoch, adapter_url):
        config = {
            "endpoint": self.endpoint,
            "region": "us-east-1",
            "access_key_id": "test",
            "secret_access_key": "test",
            "input_stream": self.input_stream,
            "output_stream": self.output_stream,
            "input_shard": "shardId-000000000000",
            "table": self.table,
            "run_id": self.run_id,
            "partition_id": self.partition_id,
            "assigned_epoch": epoch,
            "adapter_url": adapter_url,
            "poll_interval_ms": self.params["poll_interval_ms"],
            "api_timeout_seconds": self.params["api_timeout_seconds"],
            "sdk_total_attempts": self.params["sdk_total_attempts"],
            "get_records_limit": self.params["get_records_limit"],
            "hook_wait_seconds": self.params["hook_wait_seconds"],
        }
        path = os.path.join(self.workroot, "%s.json" % name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=2)
        return path

    # -- phases -----------------------------------------------------------

    def _provision(self, kinesis, dynamodb):
        cfn = client("cloudformation", self.endpoint)
        deploy(cfn, "handoff-seed-%s" % self.run_id,
               Path(__file__).parent / "cdk_seed/seed.template.json",
               InputStream=self.input_stream, OutputStream=self.output_stream,
               StateTable=self.table)
        for stream in (self.input_stream, self.output_stream):
            for _ in range(60):
                status = retryable(
                    lambda: kinesis.describe_stream(StreamName=stream), safe=True
                )["StreamDescription"]["StreamStatus"]
                if status == "ACTIVE":
                    break
                time.sleep(0.5)
            else:
                raise HarnessFailure("provision", "stream %s never became active" % stream, True)
        for _ in range(60):
            status = retryable(
                lambda: dynamodb.describe_table(TableName=self.table), safe=True
            )["Table"]["TableStatus"]
            if status == "ACTIVE":
                break
            time.sleep(0.5)
        else:
            raise HarnessFailure("provision", "table never became active", True)
        retryable(
            lambda: dynamodb.put_item(
                TableName=self.table,
                Item={
                    "pk": {"S": self.pk},
                    "sk": {"S": "CONTROL"},
                    "owner_id": {"S": "owner-1"},
                    "epoch": {"N": str(self.params["initial_epoch"])},
                    "input_checkpoint": {"S": "START"},
                    "recovery_boundary": {"S": "NONE"},
                },
            ),
            safe=True,
        )
        self._mark("provisioned streams and state table")

    def run(self):
        kinesis = client("kinesis", self.endpoint)
        dynamodb = client("dynamodb", self.endpoint)
        gate = ReadGate(self.params["poll_interval_ms"] / 1000.0)
        params = self.params
        try:
            self._provision(kinesis, dynamodb)

            warmup = ["w-%s" % uuid.uuid4().hex[:10] for _ in range(params["warmup_unique"])]
            tail = ["t-%s" % uuid.uuid4().hex[:10] for _ in range(params["tail_unique"])]
            target = "x-%s" % uuid.uuid4().hex[:10]
            expected = {}
            for group in (warmup, [target], tail):
                for message_id in group:
                    expected[message_id] = "body-%s" % uuid.uuid4().hex[:8]
            self.result["expected"] = expected
            self.result["target_id"] = target

            self.service = AppendService(kinesis, self.output_stream, self.partition_id, params)
            self.service.bind(self.run_id, self.partition_id)
            self.service.start()

            primary_config = self._write_config("primary", params["initial_epoch"], self.service.url)
            primary = WorkerProcess("owner-1", primary_config, params)
            self.workers.append(primary)
            if primary.wait_event("READY", params["hook_wait_seconds"]) is None:
                raise HarnessFailure("start", "the first worker never reported READY")
            primary.send({"cmd": "GO"})
            self._mark("first worker started")

            for message_id in warmup:
                for _ in range(params["copies_per_id"]):
                    self._put_input(kinesis, message_id, expected[message_id])
            if not self._wait_checkpoint(
                dynamodb, self._input_sequence, SETUP_TIMEOUT
            ):
                raise HarnessFailure(
                    "warmup",
                    "the first worker never accounted for the %d warmup records"
                    % self._input_sequence,
                )
            warmup_position = self._input_sequence
            self._mark("warmup accounted for through position %d" % warmup_position)

            # One message in flight, held at the pause the worker announces.
            primary.arm_hold()
            target_position = self._put_input(kinesis, target, expected[target])
            announced = primary.wait_event(
                "AFTER_SEEN_BEFORE_PUBLISH",
                params["hook_wait_seconds"],
                match={"messageId": target},
            )
            if announced is None or announced.get("messageId") != target:
                raise HarnessFailure(
                    "inject",
                    "the first worker never announced the pause for %r" % target,
                )
            row = retryable(
                lambda: dynamodb.get_item(
                    TableName=self.table,
                    Key={"pk": {"S": self.pk}, "sk": {"S": "SEEN#%s" % target}},
                    ConsistentRead=True,
                ),
                safe=True,
            ).get("Item")
            control = self._control(dynamodb)
            if (
                row is None
                or self._scalar(row, "publication_state") != "PENDING"
                or self._scalar(row, "input_sequence") != str(target_position)
                or self._scalar(control, "input_checkpoint") != str(warmup_position)
            ):
                raise HarnessFailure(
                    "inject",
                    "the partition state at the pause was not what the drill expects",
                )
            self._mark("pause announced for the in-flight message at position %d" % target_position)

            if self.variant == "before_append":
                primary.kill()
                self._mark("first worker killed before any append was submitted")
            elif self.variant == "committed_append":
                self.service.arm_hold()
                primary.send({"cmd": "CONTINUE"})
                if not self.service.wait_committed(params["api_timeout_seconds"] * 2 + 5):
                    raise HarnessFailure(
                        "inject", "the append for the in-flight message never committed", True
                    )
                primary.kill()
                self._mark(
                    "append committed for the in-flight message, response withheld, "
                    "first worker killed"
                )
            else:
                self.service.arm_defer()
                primary.send({"cmd": "CONTINUE"})
                if not self.service.wait_committed(params["api_timeout_seconds"] * 2 + 5):
                    raise HarnessFailure(
                        "inject",
                        "the append for the in-flight message was never accepted",
                        True,
                    )
                primary.kill()
                self._mark(
                    "append accepted with its record still unwritten, "
                    "first worker killed"
                )

            if not primary.reap(params["join_timeout_seconds"]):
                raise HarnessFailure("inject", "the first worker did not exit", True)
            if self.variant != "unwritten_append":
                self.service.release()
                if not self.service.drain(params["adapter_drain_timeout_seconds"]):
                    raise HarnessFailure(
                        "inject", "accepted appends did not settle before the boundary", True
                    )
            boundary_id = uuid.uuid4().hex
            boundary = self.service.append_barrier(boundary_id)
            try:
                dynamodb.update_item(
                    TableName=self.table,
                    Key={"pk": {"S": self.pk}, "sk": {"S": "CONTROL"}},
                    UpdateExpression=(
                        "SET epoch = :epoch, owner_id = :owner, "
                        "recovery_boundary = :boundary"
                    ),
                    ConditionExpression="epoch = :old_epoch AND owner_id = :old_owner",
                    ExpressionAttributeValues={
                        ":epoch": {"N": str(params["replacement_epoch"])},
                        ":owner": {"S": "owner-2"},
                        ":boundary": {"S": boundary_id},
                        ":old_epoch": {"N": str(params["initial_epoch"])},
                        ":old_owner": {"S": "owner-1"},
                    },
                )
            except ClientError as error:
                if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
                    raise HarnessFailure(
                        "inject", "the partition could not be reassigned", True
                    )
                raise
            self._mark(
                "predecessor reaped, %s, boundary %s appended at %s"
                % (
                    "appends drained"
                    if self.variant != "unwritten_append"
                    else "one accepted append still unwritten",
                    boundary_id[:8],
                    boundary["sequence"][:12],
                )
            )

            spare_config = self._write_config(
                "spare", params["replacement_epoch"], self.service.url
            )
            spare = WorkerProcess("owner-2", spare_config, params)
            self.workers.append(spare)
            if spare.wait_event("READY", params["hook_wait_seconds"]) is None:
                raise HarnessFailure("replace", "the replacement never reported READY")

            self.observer = Observer(
                kinesis, self.output_stream, gate, params["get_records_limit"], target
            )
            started = time.monotonic()
            spare.send({"cmd": "GO"})
            self.observer.start()
            if self.variant == "unwritten_append":
                # The service finishes a request it has accepted on its own
                # schedule; the replacement is already running when this one's
                # record reaches the stream.
                release = threading.Timer(
                    params["accepted_release_seconds"], self.service.release
                )
                release.daemon = True
                release.start()
            self._mark("replacement started; the rest of the input was released")
            self._put_input(kinesis, target, expected[target])
            for message_id in tail:
                for _ in range(params["copies_per_id"]):
                    self._put_input(kinesis, message_id, expected[message_id])
            final_position = self._input_sequence
            self.result["final_position"] = final_position

            settled = self._wait_checkpoint(
                dynamodb, final_position, params["completion_deadline_seconds"]
            )
            completion_elapsed = time.monotonic() - started
            spare.send({"cmd": "QUIESCE"})
            quiesced = spare.wait_event(
                "QUIESCED", max(params["completion_deadline_seconds"] - completion_elapsed, 5)
            )
            self.result["quiesced"] = quiesced is not None
            spare.close()
            self._mark(
                "replacement accounted through position %d (%s of %d), quiesced=%s"
                % (
                    final_position,
                    "yes" if settled else "no",
                    params["completion_deadline_seconds"],
                    self.result["quiesced"],
                )
            )

            # The count is taken on the stream, so nothing is counted until the
            # append service has finished every request it accepted: a request
            # taken before the replacement started still owes the stream its
            # record, and counting before it lands would grade an unfinished
            # stream.
            if not self.service.drain(params["completion_deadline_seconds"]):
                raise HarnessFailure(
                    "scan",
                    "the append service never finished the requests it accepted",
                    True,
                )
            self.observer.stop()
            self.result["target_first_seen"] = self.observer.first_seen
            quiet_started = time.monotonic()
            time.sleep(params["quiet_observation_seconds"])
            final_boundary_id = uuid.uuid4().hex
            self.service.append_barrier(final_boundary_id)
            records = read_output(
                kinesis,
                self.output_stream,
                gate,
                params["get_records_limit"],
                quiet_started + params["verification_deadline_seconds"],
                final_boundary_id,
            )
            self.result["records"] = records

            control = self._control(dynamodb)
            self.result["final_checkpoint"] = self._scalar(control, "input_checkpoint")
            scan = retryable(
                lambda: dynamodb.query(
                    TableName=self.table,
                    KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
                    ExpressionAttributeValues={
                        ":pk": {"S": self.pk},
                        ":prefix": {"S": "SEEN#"},
                    },
                    ConsistentRead=True,
                ),
                safe=True,
            )
            for item in scan.get("Items", []):
                message_id = item["sk"]["S"][len("SEEN#"):]
                self.result["rows"][message_id] = {
                    "publication_state": self._scalar(item, "publication_state"),
                    "seen": self._scalar(item, "seen"),
                    "payload": self._scalar(item, "payload"),
                    "input_sequence": self._scalar(item, "input_sequence"),
                }
            self.result["appended_ids"] = self.service.appended_message_ids()
            self.result["service_sequences"] = [
                entry["sequence"]
                for entry in self.service.trace()
                if entry["record"].get("kind") == "DATA"
            ]
            self.result["ok"] = True
            self.result["phase"] = "done"
            self._mark(
                "scan read %d output records through the final boundary"
                % len(records)
            )
        except HarnessFailure as failure:
            self.result["phase"] = failure.phase
            self.result["reason"] = str(failure)
            self.result["fixture"] = failure.fixture
            if self.observer is not None:
                self.observer.stop()
                self.result["target_first_seen"] = self.observer.first_seen
            self._mark("trial stopped in phase %r: %s" % (failure.phase, failure))
        except Exception as error:  # never leave a trial unscorable
            self.result["phase"] = "unexpected"
            self.result["reason"] = "%s: %s" % (type(error).__name__, error)
            self.result["fixture"] = True
            if self.observer is not None:
                self.observer.stop()
            self._mark("trial stopped unexpectedly: %s" % self.result["reason"])
        finally:
            self.result["counts"] = Counter(
                record["messageId"]
                for record in self.result["records"]
                if record.get("kind") == "DATA"
            )
            for worker in self.workers:
                self.result["events"].extend(
                    {"worker": worker.worker_id, "event": event} for event in worker.events
                )
                self.result["stderr"].extend(
                    {"worker": worker.worker_id, "line": line}
                    for line in worker.stderr_tail
                )
                worker.close()
            if self.service is not None:
                self.service.release()
                self.service.stop()
            for stream in (self.input_stream, self.output_stream):
                try:
                    kinesis.delete_stream(StreamName=stream)
                except Exception:
                    pass
            try:
                dynamodb.delete_table(TableName=self.table)
            except Exception:
                pass
        return self.result
