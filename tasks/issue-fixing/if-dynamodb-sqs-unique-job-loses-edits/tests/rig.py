"""Verifier harness: per-test queue and tables, worker processes, dispatch runs, ledger reads.

Nothing here imports /app. Workers run as separate processes with PYTHONPATH=/app (or the current
release, for the fleet being replaced); dispatches always run the current release, as the catalog
service does. Each process gets its own fresh working directory.
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from cdk_seed.cfn_deploy import deploy

import boto3

ENDPOINT = os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566")
REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.environ.get("REFRESHER_APP", "/app")
CURRENT_RELEASE = os.path.join(HERE, "legacy")   # the release in production (= the starter)
QUEUE_PREFIX = "refresh-jobs-v"

UNIQUE_FOR = 600
RELEASE_AFTER = 5
BACKOFF = 2
MAX_TRIES = 10
BARRIER_TIMEOUT = 60
START_WAIT = 25


def ddb():
    return boto3.client("dynamodb", endpoint_url=ENDPOINT, region_name=REGION)


def sqs():
    return boto3.client("sqs", endpoint_url=ENDPOINT, region_name=REGION)


def _create_table(client, name, range_key=False):
    attrs = [{"AttributeName": "pk", "AttributeType": "S"}]
    keys = [{"AttributeName": "pk", "KeyType": "HASH"}]
    if range_key:
        attrs.append({"AttributeName": "sk", "AttributeType": "S"})
        keys.append({"AttributeName": "sk", "KeyType": "RANGE"})
    deploy(boto3.client("cloudformation", endpoint_url=client.meta.endpoint_url,
                        region_name=client.meta.region_name),
           "refresher-table-" + name.replace("_", "-") + "-" + uuid.uuid4().hex,
           Path(__file__).parent / "cdk_seed" / ("range.template.json" if range_key else "seed.template.json"),
           TableName=name)
    for _ in range(100):
        status = client.describe_table(TableName=name)["Table"].get("TableStatus")
        if status in (None, "ACTIVE"):
            return
        time.sleep(0.1)


class Rig:
    def __init__(self, label):
        self.label = label
        self.suffix = uuid.uuid4().hex[:8]
        self.ddb = ddb()
        self.sqs = sqs()
        self.unique_table = f"u_{self.suffix}"
        self.overlap_table = f"o_{self.suffix}"
        self.entity_table = f"e_{self.suffix}"
        self.ledger_table = f"l_{self.suffix}"
        self.barrier_table = f"b_{self.suffix}"
        for name in (self.unique_table, self.overlap_table, self.entity_table,
                     self.barrier_table):
            _create_table(self.ddb, name)
        _create_table(self.ddb, self.ledger_table, range_key=True)
        self.queue_url = deploy(
            boto3.client("cloudformation", endpoint_url=ENDPOINT, region_name=REGION),
            "refresher-queue-" + self.suffix,
            Path(__file__).parent / "cdk_seed/queue.template.json",
            QueueName=f"{QUEUE_PREFIX}{self.suffix}")["QueueUrl"]
        self.workdir = tempfile.mkdtemp(prefix=f"rig-{label}-")
        self.handler_config = os.path.join(self.workdir, "probe.json")
        with open(self.handler_config, "w") as handle:
            json.dump({"endpoint": ENDPOINT, "region": REGION,
                       "entity_table": self.entity_table, "ledger_table": self.ledger_table,
                       "barrier_table": self.barrier_table,
                       "barrier_timeout": BARRIER_TIMEOUT, "work_seconds": 0.3}, handle)
        self.workers = []
        self.active = []
        self.timeline = []
        self.t0 = time.time()

    # ---------------------------------------------------------------- processes

    def env(self, pythonpath=APP, **overrides):
        env = dict(os.environ)
        env.update({
            "PYTHONPATH": pythonpath,
            "AWS_ENDPOINT_URL": ENDPOINT,
            "AWS_DEFAULT_REGION": REGION,
            "AWS_ACCESS_KEY_ID": os.environ.get("AWS_ACCESS_KEY_ID", "test"),
            "AWS_SECRET_ACCESS_KEY": os.environ.get("AWS_SECRET_ACCESS_KEY", "test"),
            "REFRESH_QUEUE_URL": self.queue_url,
            "UNIQUE_LOCK_TABLE": self.unique_table,
            "OVERLAP_LOCK_TABLE": self.overlap_table,
            "ENTITY_TABLE": self.entity_table,
            "UNIQUE_FOR": str(UNIQUE_FOR),
            "RELEASE_AFTER": str(RELEASE_AFTER),
            "OVERLAP_EXPIRES": "120",
            "BACKOFF": str(BACKOFF),
            "MAX_TRIES": str(MAX_TRIES),
            "RECEIVE_WAIT": "1",
            "REFRESH_CMD": f"{sys.executable} {os.path.join(HERE, 'probe_handler.py')} "
                           f"--config {self.handler_config}",
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        env.update({k: str(v) for k, v in overrides.items()})
        return env

    def start_workers(self, count=2, current_release=False, **overrides):
        """Start ``count`` workers of the fixed release (or of the release in production)."""
        path = CURRENT_RELEASE if current_release else APP
        started = []
        for _ in range(count):
            index = len(self.workers) + 1
            cwd = tempfile.mkdtemp(prefix="worker-", dir=self.workdir)
            log = open(os.path.join(cwd, "worker.log"), "w")
            proc = subprocess.Popen(
                [sys.executable, "-m", "refresher", "worker", "--id", str(index)],
                cwd=cwd, env=self.env(path, **overrides), stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True)
            self.workers.append((proc, log, cwd))
            started.append((proc, log, cwd))
        self.note(f"started {count} {'current-release' if current_release else 'fixed'} workers")
        time.sleep(3)
        for proc, _, cwd in started:
            assert proc.poll() is None, (
                f"worker exited at startup with {proc.returncode}:\n" + self._log(cwd))
        self.active += started

    def _log(self, cwd):
        try:
            with open(os.path.join(cwd, "worker.log")) as handle:
                return handle.read()[-6000:]
        except OSError:
            return ""

    def _terminate(self, workers):
        for proc, _, _ in workers:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
        deadline = time.time() + 15
        for proc, log, _ in workers:
            try:
                proc.wait(timeout=max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            log.close()

    def stop_workers(self):
        """SIGTERM the running workers and wait for them, as a fleet replacement does."""
        stopping, self.active = self.active, []
        for proc, _, _ in stopping:
            assert proc.poll() is None, "a worker process died during the run"
        self._terminate(stopping)
        self.note("stopped the running workers")

    def stop(self):
        self._terminate([w for w in self.workers if not w[1].closed])

    def report(self):
        lines = [f"--- timeline ({self.label}) ---"]
        lines += [f"{t:7.2f}s {what}" for t, what in self.timeline]
        for index, (_, _, cwd) in enumerate(self.workers):
            lines.append(f"--- worker {index + 1} log ---")
            lines.append(self._log(cwd))
        return "\n".join(lines)

    def note(self, what):
        self.timeline.append((time.time() - self.t0, what))

    def workers_alive(self):
        return all(proc.poll() is None for proc, _, _ in self.active)

    # ---------------------------------------------------------------- driver actions

    def edit(self, entity):
        self.ddb.update_item(TableName=self.entity_table, Key={"pk": {"S": entity}},
                             UpdateExpression="ADD dirty_seq :one",
                             ExpressionAttributeValues={":one": {"N": "1"}})

    def dispatch(self, entity, delay=None, unique_for=None, tag=""):
        """An edit followed by a dispatch, as the catalog service does: with the release in
        production, which this fix does not redeploy."""
        self.edit(entity)
        argv = [sys.executable, "-m", "refresher", "dispatch", "--entity", entity]
        if delay is not None:
            argv += ["--delay", str(delay)]
        if unique_for is not None:
            argv += ["--unique-for", str(unique_for)]
        result = subprocess.run(argv, cwd=tempfile.mkdtemp(dir=self.workdir),
                                env=self.env(CURRENT_RELEASE), capture_output=True, text=True,
                                timeout=60)
        outcome = None
        for line in result.stdout.splitlines():
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict) and "status" in parsed:
                outcome = parsed
        self.note(f"dispatch {tag or entity} -> {outcome} (exit {result.returncode})"
                  + (f" stderr={result.stderr[-500:]!r}" if result.returncode else ""))
        assert outcome is not None and result.returncode == 0, (
            f"dispatch {tag} printed no status line (exit {result.returncode}):\n"
            f"{result.stdout}\n{result.stderr}")
        return outcome

    def barrier(self, entity, ordinal, fail=False):
        self.ddb.put_item(TableName=self.barrier_table, Item={
            "pk": {"S": f"{entity}#{ordinal}"}, "gate_open": {"BOOL": False}, "fail_run": {"BOOL": fail}})

    def go(self, entity, ordinal):
        self.note(f"go {entity}#{ordinal}")
        self.ddb.update_item(TableName=self.barrier_table,
                             Key={"pk": {"S": f"{entity}#{ordinal}"}},
                             UpdateExpression="SET gate_open = :t",
                             ExpressionAttributeValues={":t": {"BOOL": True}})

    # ---------------------------------------------------------------- observations

    def ledger(self, entity):
        rows, kwargs = [], {}
        while True:
            page = self.ddb.query(TableName=self.ledger_table, ConsistentRead=True,
                                  KeyConditionExpression="pk = :pk",
                                  ExpressionAttributeValues={":pk": {"S": entity}}, **kwargs)
            for item in page["Items"]:
                ordinal, event = item["sk"]["S"].split("#")
                rows.append({"ordinal": int(ordinal), "event": event,
                             "message_id": item["message_id"]["S"],
                             "attempt": item["attempt"]["S"],
                             "seen": int(item["seen"]["N"]), "t": float(item["t"]["N"]),
                             "outcome": item.get("outcome", {}).get("S")})
            if "LastEvaluatedKey" not in page:
                return rows
            kwargs = {"ExclusiveStartKey": page["LastEvaluatedKey"]}

    def starts(self, entity):
        return sorted((r for r in self.ledger(entity) if r["event"] == "start"),
                      key=lambda r: r["ordinal"])

    def wait_start(self, entity, ordinal, timeout=START_WAIT, what=""):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for row in self.starts(entity):
                if row["ordinal"] == ordinal:
                    self.note(f"start #{ordinal} {entity} message={row['message_id'][:8]} "
                              f"attempt={row['attempt']}")
                    return row
            time.sleep(0.1)
        self.note(f"no start #{ordinal} for {entity} within {timeout}s")
        raise AssertionError(
            f"{what or f'refresh #{ordinal} of {entity}'} did not start within {timeout}s; "
            f"starts so far: {[(r['ordinal'], r['message_id'][:8]) for r in self.starts(entity)]}")

    def wait_end(self, entity, ordinal, timeout=START_WAIT):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for row in self.ledger(entity):
                if row["ordinal"] == ordinal and row["event"] == "end":
                    return row
            time.sleep(0.1)
        raise AssertionError(f"refresh #{ordinal} of {entity} did not finish within {timeout}s")

    def seqs(self, entity):
        item = self.ddb.get_item(TableName=self.entity_table, Key={"pk": {"S": entity}},
                                 ConsistentRead=True).get("Item", {})
        return (int(item.get("dirty_seq", {}).get("N", "0")),
                int(item.get("processed_seq", {}).get("N", "0")))

    def wait_fresh(self, entity, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            dirty, processed = self.seqs(entity)
            if dirty == processed:
                return
            time.sleep(0.2)
        dirty, processed = self.seqs(entity)
        raise AssertionError(f"{entity} stale: dirty_seq={dirty} processed_seq={processed} "
                             f"{timeout}s after the last expected refresh")

    def sleep_until(self, t):
        delay = t - time.time()
        if delay > 0:
            time.sleep(delay)

    def close_out(self, entity, next_ordinal, accepted_ids):
        """Every accepted job ran and nothing else did; then the entity is fresh and a new edit
        is still accepted and refreshed."""
        starts = self.starts(entity)
        unknown = [r for r in starts if r["message_id"] not in accepted_ids]
        assert not unknown, (
            f"refreshes ran for messages no dispatch reported: "
            f"{[(r['ordinal'], r['message_id']) for r in unknown]}")
        self.wait_end(entity, next_ordinal - 1)
        self.wait_fresh(entity)
        probe = self.dispatch(entity, tag="closing edit")
        assert probe["status"] == "ACCEPTED", (
            f"an edit made after every queued refresh of {entity} had run was dropped: the "
            f"entity's dedup lock was still held with no refresh waiting")
        row = self.wait_start(entity, next_ordinal, what="the closing edit's refresh")
        assert row["message_id"] == probe["message_id"]
        self.wait_end(entity, next_ordinal)
        self.wait_fresh(entity)
        assert self.workers_alive(), "a worker process died during the run"
