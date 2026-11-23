#!/usr/bin/env python3
"""Partition worker for the delivery dedup pipeline.

The worker owns one partition of the input stream. It reads the partition's
input from its checkpoint, keeps the partition's seen-key state in the state
table, and publishes accepted messages to the output stream through the local
append service.

Usage:
    worker.py --config <config.json> --worker-id <id>

The control protocol, the state contract and the append service API are in
/app/CONTRACT.md. Diagnostics go to stderr; stdout carries the control channel
and must stay JSON-lines only.
"""

from __future__ import annotations

import argparse
import json
import select
import sys
import time
import urllib.error
import urllib.request

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

PENDING = "PENDING"
CONFIRMED = "CONFIRMED"
CHECKPOINT_START = "START"
BOUNDARY_NONE = "NONE"
SEEN_PREFIX = "SEEN#"


class WorkerError(RuntimeError):
    """Raised when the worker cannot continue under its assignment."""


def note(message):
    print(message, file=sys.stderr, flush=True)


def service_config(config):
    """Budget for the state table and the streams. The append service has its
    own, tighter budget in `api_timeout_seconds`; a stream or a table that
    takes a moment to answer is not a decision about a message."""
    budget = max(10, int(config["api_timeout_seconds"]))
    return Config(
        retries={"total_max_attempts": int(config["sdk_total_attempts"])},
        connect_timeout=min(5, budget),
        read_timeout=budget,
    )


def make_client(config, service):
    return boto3.client(
        service,
        endpoint_url=config["endpoint"],
        region_name=config["region"],
        aws_access_key_id=config["access_key_id"],
        aws_secret_access_key=config["secret_access_key"],
        config=service_config(config),
    )


SHARD_IS_BUSY = {
    "ProvisionedThroughputExceededException",
    "ThrottlingException",
    "ExpiredIteratorException",
    "InternalServerError",
}


def read_page(client, iterator, limit, restart, interval, attempts=6):
    """One GetRecords call. A busy shard is waited out, a stale iterator is
    restarted from the start of the shard; anything else is a real failure.

    Returns the response and the iterator to use next.
    """
    for attempt in range(attempts):
        try:
            response = client.get_records(ShardIterator=iterator, Limit=limit)
            return response, response.get("NextShardIterator", iterator)
        except ClientError as error:
            code = error.response["Error"]["Code"]
            if code not in SHARD_IS_BUSY or attempt + 1 == attempts:
                raise
            if code == "ExpiredIteratorException":
                iterator = restart()
            else:
                time.sleep(interval)
    raise RuntimeError("unreachable")


class ControlChannel:
    """JSON-lines channel to the partition supervisor."""

    def __init__(self, infile, outfile):
        self._in = infile
        self._out = outfile

    def emit(self, event, **fields):
        self._out.write(json.dumps({"event": event, **fields}) + "\n")
        self._out.flush()

    def _readline(self, timeout):
        try:
            readable, _, _ = select.select([self._in.fileno()], [], [], timeout)
        except (OSError, ValueError):
            return None
        if not readable:
            return None
        line = self._in.readline()
        if not line:
            return None
        text = line.strip()
        return text or None

    def wait_command(self, name, timeout):
        """Block until the named command arrives; None on timeout or EOF."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            text = self._readline(remaining)
            if text is None:
                return None
            try:
                message = json.loads(text)
            except ValueError:
                continue
            if message.get("cmd") == name:
                return message

    def poll_command(self, timeout):
        """Return the next command if one arrives within the timeout."""
        text = self._readline(timeout)
        if text is None:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return None


class StateStore:
    """The partition's durable state. Item shapes are fixed by CONTRACT.md."""

    def __init__(self, config, resource):
        self._table = resource.Table(config["table"])
        self._pk = "RUN#%s#PART#%s" % (config["run_id"], config["partition_id"])
        self._owner = config["worker_id"]
        self._epoch = int(config["assigned_epoch"])

    def load_control(self):
        response = self._table.get_item(
            Key={"pk": self._pk, "sk": "CONTROL"}, ConsistentRead=True
        )
        return response.get("Item")

    def load_seen(self):
        response = self._table.query(
            KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
            ExpressionAttributeValues={":pk": self._pk, ":prefix": SEEN_PREFIX},
            ConsistentRead=True,
        )
        rows = {}
        for item in response.get("Items", []):
            rows[item["sk"][len(SEEN_PREFIX):]] = item
        return rows

    def insert_seen(self, message_id, input_sequence, payload):
        """Create the partition's row for a key, or None if it already exists."""
        item = {
            "pk": self._pk,
            "sk": SEEN_PREFIX + message_id,
            "seen": 1,
            "input_sequence": str(input_sequence),
            "payload": payload,
            "publication_state": PENDING,
            "version": 1,
            "updated_by": self._owner,
        }
        try:
            self._table.put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
        except ClientError as error:
            if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return None
            raise
        return item

    def confirm_seen(self, message_id, expected_version, output_sequence=None):
        """Record the publication outcome on an existing row."""
        expression = "SET publication_state = :state, version = :next, updated_by = :owner"
        values = {
            ":state": CONFIRMED,
            ":next": int(expected_version) + 1,
            ":expected": int(expected_version),
            ":owner": self._owner,
        }
        if output_sequence is not None:
            expression += ", observed_output_sequence = :seq"
            values[":seq"] = str(output_sequence)
        try:
            self._table.update_item(
                Key={"pk": self._pk, "sk": SEEN_PREFIX + message_id},
                UpdateExpression=expression,
                ConditionExpression="version = :expected",
                ExpressionAttributeValues=values,
            )
        except ClientError as error:
            if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise
        return True

    def advance_checkpoint(self, previous, following):
        """Move the partition's input checkpoint forward by one position."""
        try:
            self._table.update_item(
                Key={"pk": self._pk, "sk": "CONTROL"},
                UpdateExpression="SET input_checkpoint = :ckpt",
                ConditionExpression=(
                    "epoch = :epoch AND owner_id = :owner AND input_checkpoint = :prev"
                ),
                ExpressionAttributeValues={
                    ":ckpt": str(following),
                    ":epoch": self._epoch,
                    ":owner": self._owner,
                    ":prev": str(previous),
                },
            )
        except ClientError as error:
            if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise
        return True


class InputStream:
    """Sequential reader for the partition's input shard."""

    def __init__(self, config, client):
        self._client = client
        self._stream = config["input_stream"]
        self._shard = config["input_shard"]
        self._limit = int(config["get_records_limit"])
        self._interval = int(config["poll_interval_ms"]) / 1000.0
        self._iterator = None

    def _start(self):
        response = self._client.get_shard_iterator(
            StreamName=self._stream,
            ShardId=self._shard,
            ShardIteratorType="TRIM_HORIZON",
        )
        self._iterator = response["ShardIterator"]
        return self._iterator

    def fetch(self):
        if self._iterator is None:
            self._start()
        response, self._iterator = read_page(
            self._client,
            self._iterator,
            self._limit,
            self._start,
            self._interval,
        )
        records = []
        for record in response.get("Records", []):
            records.append(json.loads(record["Data"].decode("utf-8")))
        return records


class OutputLog:
    """Reader for the partition's output stream, which is what was delivered."""

    def __init__(self, config, client, append):
        self._client = client
        self._append = append
        self._stream = config["output_stream"]
        self._shard = config["input_shard"]
        self._limit = int(config["get_records_limit"])
        self._interval = int(config["poll_interval_ms"]) / 1000.0

    def _restart(self):
        response = self._client.get_shard_iterator(
            StreamName=self._stream,
            ShardId=self._shard,
            ShardIteratorType="TRIM_HORIZON",
        )
        return response["ShardIterator"]

    def _page(self, iterator):
        response, iterator = read_page(
            self._client, iterator, self._limit, self._restart, self._interval
        )
        records = []
        for record in response.get("Records", []):
            records.append(json.loads(record["Data"].decode("utf-8")))
        return records, iterator

    def established_ids(self, boundary, budget):
        """Ids whose records are in the output stream.

        The first pass reads from the start of the shard to the boundary record
        that was written when the partition changed hands. That is where the
        handover fell in the stream, not what the append service had finished by
        then: a request it accepted beforehand is answered only once its record
        is in the stream, and it may still be holding one. So the read carries
        on from where it stands for as long as the service holds an accepted
        request, and an empty page ends it only once the service holds none.
        """
        iterator = self._client.get_shard_iterator(
            StreamName=self._stream,
            ShardId=self._shard,
            ShardIteratorType="TRIM_HORIZON",
        )["ShardIterator"]
        ids = set()
        idle = 0
        handed_over = boundary == BOUNDARY_NONE
        deadline = time.monotonic() + budget
        while True:
            holding = self._append.pending()
            records, iterator = self._page(iterator)
            for record in records:
                if record.get("kind") == "BARRIER":
                    if record.get("barrier_id") == boundary:
                        handed_over = True
                    continue
                if record.get("messageId") is not None:
                    ids.add(record["messageId"])
            idle = idle + 1 if not records else 0
            if holding == 0 and handed_over and idle >= 1:
                return ids
            if time.monotonic() >= deadline:
                if self._append.pending() == 0:
                    return ids
                raise WorkerError(
                    "the append service still holds an accepted request"
                )
            time.sleep(self._interval)


class AppendService:
    """HTTP client for the local append service."""

    def __init__(self, config):
        self._url = str(config["adapter_url"]).rstrip("/")
        self._timeout = int(config["api_timeout_seconds"])

    def publish(self, request):
        body = json.dumps(request).encode("utf-8")
        call = urllib.request.Request(
            self._url + "/publish",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(call, timeout=self._timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")
            raise WorkerError("append service rejected the request: %s" % detail)

    def pending(self):
        """How many requests the service has accepted and not answered.

        A request is answered only once its record is in the stream, so while
        this is above zero the stream is not yet the whole truth about what has
        been delivered. A service that cannot be reached says nothing either
        way, so one failed ask is tried again before it is read as a zero.
        """
        for attempt in (0, 1):
            call = urllib.request.Request(self._url + "/pending", method="GET")
            try:
                with urllib.request.urlopen(call, timeout=self._timeout) as response:
                    answer = json.loads(response.read().decode("utf-8"))
            except (urllib.error.URLError, OSError, ValueError) as error:
                if attempt:
                    note("the append service did not answer /pending: %s" % error)
                    return 0
                time.sleep(min(1.0, self._timeout))
                continue
            try:
                return int(answer.get("accepted", 0))
            except (TypeError, ValueError):
                return 0


class PartitionWorker:
    def __init__(self, config, worker_id, channel):
        self.config = config
        self.worker_id = worker_id
        self.channel = channel
        self.epoch = int(config["assigned_epoch"])
        self.partition_id = config["partition_id"]
        self.run_id = config["run_id"]
        self.poll_interval = int(config["poll_interval_ms"]) / 1000.0
        self.hook_wait = int(config["hook_wait_seconds"])

        client = make_client(config, "kinesis")
        resource = boto3.resource(
            "dynamodb",
            endpoint_url=config["endpoint"],
            region_name=config["region"],
            aws_access_key_id=config["access_key_id"],
            aws_secret_access_key=config["secret_access_key"],
            config=service_config(config),
        )
        self.store = StateStore(config, resource)
        self.input = InputStream(config, client)
        self.append = AppendService(config)
        self.output = OutputLog(config, client, self.append)

        self.checkpoint = CHECKPOINT_START
        self.recovery_boundary = BOUNDARY_NONE
        self.seen = {}
        self.published = set()

    # -- lifecycle ------------------------------------------------------

    def run(self):
        self._load_assignment()
        self.channel.emit("READY", epoch=str(self.epoch), checkpoint=self.checkpoint)
        if self.channel.wait_command("GO", self.hook_wait) is None:
            note("no GO received before the wait expired")
            return 3
        self._recover()
        self._serve()
        self.channel.emit("QUIESCED")
        return 0

    def _load_assignment(self):
        control = self.store.load_control()
        if control is None:
            raise WorkerError("no control row for partition %s" % self.partition_id)
        if int(control.get("epoch", -1)) != self.epoch:
            raise WorkerError(
                "control row epoch %s does not match assignment %s"
                % (control.get("epoch"), self.epoch)
            )
        if control.get("owner_id") != self.worker_id:
            raise WorkerError(
                "control row owner %r does not match assignment %r"
                % (control.get("owner_id"), self.worker_id)
            )
        self.checkpoint = control.get("input_checkpoint", CHECKPOINT_START)
        self.recovery_boundary = control.get("recovery_boundary", BOUNDARY_NONE)
        note(
            "assignment loaded: partition=%s epoch=%s checkpoint=%s"
            % (self.partition_id, self.epoch, self.checkpoint)
        )

    def _recover(self):
        """Adopt the partition's durable state before serving input.

        The rows this process did not write say a key was accepted, not that it
        was delivered, so the delivered set is read from the output stream. It
        is read only as far as the append service has nothing still outstanding:
        a request the service accepted before this worker started is answered
        once its record is in the stream, which can be after this worker starts.
        """
        control = self.store.load_control()
        if control is not None:
            self.checkpoint = control.get("input_checkpoint", self.checkpoint)
            self.recovery_boundary = control.get(
                "recovery_boundary", self.recovery_boundary
            )
        self.seen = self.store.load_seen()
        unconfirmed = [
            (message_id, row)
            for message_id, row in self.seen.items()
            if row.get("publication_state") != CONFIRMED
        ]
        if not unconfirmed and self.recovery_boundary == BOUNDARY_NONE:
            note("adopted %d seen rows, nothing to settle" % len(self.seen))
            return
        self.published = self.output.established_ids(
            self.recovery_boundary, self.hook_wait * 3
        )
        for message_id, row in unconfirmed:
            if message_id in self.published:
                self.store.confirm_seen(message_id, int(row["version"]), None)
                self.seen[message_id] = dict(
                    row, publication_state=CONFIRMED, version=int(row["version"]) + 1
                )
                note("key %s was already delivered; row confirmed" % message_id)
                continue
            note("key %s was accepted but not delivered; publishing" % message_id)
            self._publish(int(row["input_sequence"]), message_id, row)
        note(
            "adopted %d seen rows for partition %s"
            % (len(self.seen), self.partition_id)
        )

    def _serve(self):
        while True:
            command = self.channel.poll_command(self.poll_interval)
            if command is not None and command.get("cmd") == "QUIESCE":
                return
            for record in self.input.fetch():
                if record.get("kind") != "DATA":
                    continue
                self._admit(record)

    # -- per-message path -------------------------------------------------

    @staticmethod
    def _position(checkpoint):
        if checkpoint in (None, CHECKPOINT_START):
            return 0
        return int(checkpoint)

    def _admit(self, record):
        """Apply the partition's admission rule to one input record."""
        message_id = record["messageId"]
        input_sequence = int(record["input_sequence"])
        if input_sequence <= self._position(self.checkpoint):
            return
        row = self.seen.get(message_id)
        if row is not None:
            if row.get("publication_state") == CONFIRMED or message_id in self.published:
                self._suppress(input_sequence, message_id)
                self._advance(input_sequence)
                return
        else:
            created = self.store.insert_seen(message_id, input_sequence, record["payload"])
            if created is None:
                self._suppress(input_sequence, message_id)
                self._advance(input_sequence)
                return
            row = created
        self._publish(input_sequence, message_id, row)
        self._advance(input_sequence)

    def _publish(self, input_sequence, message_id, row):
        self.channel.emit(
            "AFTER_SEEN_BEFORE_PUBLISH",
            input_sequence=str(input_sequence),
            messageId=message_id,
        )
        if self.channel.wait_command("CONTINUE", self.hook_wait) is None:
            raise WorkerError("no CONTINUE received for the publication pause")
        receipt = self.append.publish(
            {
                "run_id": self.run_id,
                "partition_id": self.partition_id,
                "epoch": str(self.epoch),
                "input_sequence": str(input_sequence),
                "messageId": message_id,
                "payload": row["payload"],
            }
        )
        version = int(row["version"])
        self.store.confirm_seen(message_id, version, receipt.get("sequence"))
        self.seen[message_id] = dict(
            row, publication_state=CONFIRMED, version=version + 1
        )
        self.published.add(message_id)
        self.channel.emit(
            "PUBLISHED",
            input_sequence=str(input_sequence),
            messageId=message_id,
            sequence=str(receipt.get("sequence")),
        )

    def _suppress(self, input_sequence, message_id):
        self.channel.emit(
            "SUPPRESSED", input_sequence=str(input_sequence), messageId=message_id
        )

    def _advance(self, input_sequence):
        if not self.store.advance_checkpoint(self.checkpoint, input_sequence):
            raise WorkerError(
                "checkpoint move from %s to %s was rejected" % (self.checkpoint, input_sequence)
            )
        self.checkpoint = str(input_sequence)


def main(argv=None):
    parser = argparse.ArgumentParser(description="delivery dedup partition worker")
    parser.add_argument("--config", required=True, help="path to the worker config JSON")
    parser.add_argument("--worker-id", required=True, dest="worker_id")
    args = parser.parse_args(argv)

    with open(args.config, "r", encoding="utf-8") as handle:
        config = json.load(handle)
    config["worker_id"] = args.worker_id

    channel = ControlChannel(sys.stdin, sys.stdout)
    worker = PartitionWorker(config, args.worker_id, channel)
    try:
        return worker.run()
    except WorkerError as error:
        note("worker error: %s" % error)
        return 2


if __name__ == "__main__":
    sys.exit(main())
