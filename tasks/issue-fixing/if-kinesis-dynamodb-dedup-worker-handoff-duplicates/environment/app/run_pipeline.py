#!/usr/bin/env python3
"""Run one clean pass of the delivery pipeline.

Provisions a fresh input stream, output stream and state table, starts the
local append service and one worker, ingests a small batch of messages — some
of them twice — and prints what happened: the input the partition received, the
events the worker reported, and the records the output stream ended up holding.

    python3 /app/run_pipeline.py [--ids 3] [--copies 2] [--keep]

Nothing here is graded and nothing here replaces the worker; it is only a way
to watch the pieces run against the local endpoint.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

import boto3
from botocore.config import Config

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from infra.cfn_deploy import deploy

import adapter  # noqa: E402  (local module, same directory)

ENDPOINT = os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566")
REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
SHARD = "shardId-000000000000"


def client(service, config):
    """The streams and the table may take a moment to answer on a cold
    endpoint; that is not something this pass measures."""
    budget = max(10, int(config["api_timeout_seconds"]))
    return boto3.client(
        service,
        endpoint_url=config["endpoint"],
        region_name=config["region"],
        aws_access_key_id=config["access_key_id"],
        aws_secret_access_key=config["secret_access_key"],
        config=Config(
            retries={"total_max_attempts": int(config["sdk_total_attempts"])},
            connect_timeout=min(5, budget),
            read_timeout=budget,
        ),
    )


def wait_active(kinesis, name):
    for _ in range(60):
        response = kinesis.describe_stream(StreamName=name)
        status = response["StreamDescription"]["StreamStatus"]
        if status == "ACTIVE":
            return
        time.sleep(0.5)
    raise RuntimeError("stream %s never became active" % name)


class WorkerProcess:
    """Drive one worker subprocess through the control protocol."""

    def __init__(self, config_path, worker_id):
        self.events = []
        self._queue = queue.Queue()
        self._cwd = tempfile.mkdtemp(prefix="worker-")
        self.process = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "worker.py"),
             "--config", config_path, "--worker-id", worker_id],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=self._cwd,
            env=dict(os.environ, PYTHONPATH=HERE),
        )
        self._lock = threading.Lock()
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self):
        try:
            for line in self.process.stdout:
                text = line.strip()
                if not text:
                    continue
                message = json.loads(text)
                self.events.append(message)
                self._queue.put(message)
                if message.get("event") == "AFTER_SEEN_BEFORE_PUBLISH":
                    self.send({"cmd": "CONTINUE"})
        except Exception:
            pass
        finally:
            self._queue.put(None)

    def _read_stderr(self):
        for line in self.process.stderr:
            sys.stderr.write("[worker] %s" % line)

    def send(self, command):
        with self._lock:
            try:
                self.process.stdin.write(json.dumps(command) + "\n")
                self.process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass

    def wait_event(self, name, timeout):
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                message = self._queue.get(timeout=remaining)
            except queue.Empty:
                return None
            if message is None:
                return None
            if message.get("event") == name:
                return message

    def close(self):
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)
        shutil.rmtree(self._cwd, ignore_errors=True)


def read_stream(kinesis, name, limit, interval, pages=200):
    iterator = kinesis.get_shard_iterator(
        StreamName=name, ShardId=SHARD, ShardIteratorType="TRIM_HORIZON"
    )["ShardIterator"]
    records = []
    for _ in range(pages):
        response = kinesis.get_records(ShardIterator=iterator, Limit=limit)
        iterator = response.get("NextShardIterator", iterator)
        for record in response.get("Records", []):
            records.append(json.loads(record["Data"].decode("utf-8")))
        if not response.get("Records"):
            break
        time.sleep(interval)
    return records


def main(argv=None):
    parser = argparse.ArgumentParser(description="run one clean pass of the pipeline")
    parser.add_argument("--ids", type=int, default=3, help="distinct message ids to send")
    parser.add_argument("--copies", type=int, default=2, help="copies of each id to send")
    parser.add_argument("--keep", action="store_true", help="leave the run in place")
    args = parser.parse_args(argv)

    run_id = "local-%s" % uuid.uuid4().hex[:10]
    partition_id = "p0"
    config = {
        "endpoint": ENDPOINT,
        "region": REGION,
        "access_key_id": os.environ.get("AWS_ACCESS_KEY_ID", "test"),
        "secret_access_key": os.environ.get("AWS_SECRET_ACCESS_KEY", "test"),
        "input_stream": "%s-in" % run_id,
        "output_stream": "%s-out" % run_id,
        "input_shard": SHARD,
        "table": "%s-state" % run_id,
        "run_id": run_id,
        "partition_id": partition_id,
        "assigned_epoch": 1,
        "poll_interval_ms": 250,
        "api_timeout_seconds": 2,
        "sdk_total_attempts": 1,
        "get_records_limit": 2,
        "hook_wait_seconds": 20,
    }

    kinesis = client("kinesis", config)
    dynamodb = client("dynamodb", config)
    deploy(client("cloudformation", config), "pipeline-seed-%s" % run_id,
           Path(HERE) / "infra/seed.template.json",
           InputStream=config["input_stream"], OutputStream=config["output_stream"],
           StateTable=config["table"])
    for stream in (config["input_stream"], config["output_stream"]):
        wait_active(kinesis, stream)
    for _ in range(60):
        describe = dynamodb.describe_table(TableName=config["table"])
        if describe["Table"]["TableStatus"] == "ACTIVE":
            break
        time.sleep(0.5)
    dynamodb.put_item(
        TableName=config["table"],
        Item={
            "pk": {"S": "RUN#%s#PART#%s" % (run_id, partition_id)},
            "sk": {"S": "CONTROL"},
            "owner_id": {"S": "primary-1"},
            "epoch": {"N": "1"},
            "input_checkpoint": {"S": "START"},
            "recovery_boundary": {"S": "NONE"},
        },
    )

    server, service = adapter.serve(config)
    config = dict(config, adapter_url="http://127.0.0.1:%d" % server.server_address[1])

    workdir = tempfile.mkdtemp(prefix="run-%s-" % run_id)
    config_path = os.path.join(workdir, "worker.json")
    with open(config_path, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)

    message_ids = ["m-%s-%d" % (run_id[-6:], index) for index in range(args.ids)]
    plan = []
    for copy in range(args.copies):
        for message_id in message_ids:
            plan.append(message_id)

    checkpoint = "START"
    output = []
    worker = WorkerProcess(config_path, "primary-1")
    try:
        ready = worker.wait_event("READY", 30)
        if ready is None:
            print("worker never reported READY", file=sys.stderr)
            return 1
        worker.send({"cmd": "GO"})

        position = 0
        for message_id in plan:
            position += 1
            kinesis.put_record(
                StreamName=config["input_stream"],
                Data=json.dumps(
                    {
                        "kind": "DATA",
                        "input_sequence": str(position),
                        "messageId": message_id,
                        "payload": "payload-%s-%s" % (message_id, uuid.uuid4().hex[:6]),
                    }
                ).encode("utf-8"),
                PartitionKey=partition_id,
            )

        deadline = time.monotonic() + 60
        table = boto3.resource(
            "dynamodb",
            endpoint_url=config["endpoint"],
            region_name=config["region"],
            aws_access_key_id=config["access_key_id"],
            aws_secret_access_key=config["secret_access_key"],
            config=client("dynamodb", config).meta.config,
        ).Table(config["table"])
        checkpoint = "START"
        while time.monotonic() < deadline:
            row = table.get_item(
                Key={
                    "pk": "RUN#%s#PART#%s" % (run_id, partition_id),
                    "sk": "CONTROL",
                },
                ConsistentRead=True,
            ).get("Item")
            checkpoint = row.get("input_checkpoint", "START")
            if checkpoint == str(position):
                break
            time.sleep(0.5)

        worker.send({"cmd": "QUIESCE"})
        worker.wait_event("QUIESCED", 30)

        output = read_stream(
            kinesis, config["output_stream"],
            int(config["get_records_limit"]), config["poll_interval_ms"] / 1000.0,
        )
    finally:
        server.shutdown()
        worker.close()
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)
            for stream in (config["input_stream"], config["output_stream"]):
                try:
                    kinesis.delete_stream(StreamName=stream)
                except Exception:
                    pass
            try:
                dynamodb.delete_table(TableName=config["table"])
            except Exception:
                pass

    print("run_id           %s" % run_id)
    print("partition        %s" % partition_id)
    print("input submitted  %d records over %d ids" % (len(plan), len(message_ids)))
    print("final checkpoint %s" % checkpoint)
    print(
        "append service   %d request(s) accepted and unanswered"
        % service.pending()
    )
    print("worker events")
    for event in worker.events:
        print("  %s" % json.dumps(event, sort_keys=True))
    print("output records   %d" % len(output))
    for record in output:
        print("  %s %s" % (record.get("messageId", record.get("kind")), record.get("payload", "")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
