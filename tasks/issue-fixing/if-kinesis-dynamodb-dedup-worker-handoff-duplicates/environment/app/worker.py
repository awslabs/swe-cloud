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

        self.checkpoint = CHECKPOINT_START
        self.recovery_boundary = BOUNDARY_NONE
        self.seen = {}

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
        """Adopt the partition's durable state before serving input."""
        control = self.store.load_control()
        if control is not None:
            self.checkpoint = control.get("input_checkpoint", self.checkpoint)
            self.recovery_boundary = control.get(
                "recovery_boundary", self.recovery_boundary
            )
        self.seen = self.store.load_seen()
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
        if row is None:
            created = self.store.insert_seen(message_id, input_sequence, record["payload"])
            if created is None:
                self._suppress(input_sequence, message_id)
                self._advance(input_sequence)
                return
            self._publish(input_sequence, message_id, created)
            self._advance(input_sequence)
            return
        self._suppress(input_sequence, message_id)
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
