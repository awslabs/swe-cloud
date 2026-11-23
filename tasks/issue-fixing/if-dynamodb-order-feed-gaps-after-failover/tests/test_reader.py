"""Service-level observations; no imports from the submitted application."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid

import boto3
from botocore.config import Config
import pytest

from cdk_seed.cfn_deploy import deploy


@pytest.fixture(scope="session")
def aws():
    try:
        with urllib.request.urlopen("http://localhost:4566/_floci/health", timeout=5) as response:
            assert response.status == 200
        options = dict(endpoint_url="http://localhost:4566", region_name="us-east-1",
                       aws_access_key_id="test", aws_secret_access_key="test",
                       config=Config(connect_timeout=2, read_timeout=3,
                                     retries={"max_attempts": 2}))
        db = boto3.client("dynamodb", **options)
        streams = boto3.client("dynamodbstreams", **options)
        db.list_tables(Limit=1)
        return db, streams
    except Exception as error:
        pytest.fail("environment: AWS preflight failed: " + str(error))


class Feed:
    def __init__(self, aws, checkpoint_table=None):
        self.db, self.streams = aws
        self.token = uuid.uuid4().hex[:12]
        self.source = "orders-" + self.token
        self.checkpoints = checkpoint_table or "positions-" + self.token
        self.own_checkpoints = checkpoint_table is None
        self.puts = []
        self.workers = []
        cfn = boto3.client("cloudformation", endpoint_url=self.db.meta.endpoint_url,
                           region_name=self.db.meta.region_name,
                           aws_access_key_id="test", aws_secret_access_key="test",
                           config=self.db.meta.config)
        seed_dir = Path(__file__).parent / "cdk_seed"
        deploy(cfn, "feed-source-" + self.token, seed_dir / "seed.template.json",
               TableName=self.source)
        if self.own_checkpoints:
            deploy(cfn, "feed-checkpoints-" + self.token,
                   seed_dir / "checkpoint.template.json", TableName=self.checkpoints)
        self.arn = self.db.describe_table(TableName=self.source)["Table"]["LatestStreamArn"]
        self.shards = self.streams.describe_stream(StreamArn=self.arn)["StreamDescription"]["Shards"]
        assert len(self.shards) == 1, "environment: expected this venue's single stream shard"
        self.shard = self.shards[0]["ShardId"]

    def put(self, label):
        identity = label + "-" + self.token
        self.db.put_item(TableName=self.source,
                         Item={"id": {"S": identity}, "payload": {"S": uuid.uuid4().hex}})
        self.puts.append(identity)
        return identity

    def snapshot(self):
        rows = []
        request = dict(TableName=self.checkpoints, ConsistentRead=True,
                       KeyConditionExpression="StreamArn = :s",
                       ExpressionAttributeValues={":s": {"S": self.arn}})
        while True:
            page = self.db.query(**request)
            rows.extend(page.get("Items", []))
            if not page.get("LastEvaluatedKey"):
                return rows
            request["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def sequences(self, raw=False):
        iterator = self.streams.get_shard_iterator(
            StreamArn=self.arn, ShardId=self.shard, ShardIteratorType="TRIM_HORIZON")["ShardIterator"]
        result = {}
        deadline = time.monotonic() + 5
        while iterator and time.monotonic() < deadline:
            page = self.streams.get_records(ShardIterator=iterator, Limit=100)
            for record in page.get("Records", []):
                data = record["dynamodb"]
                result[data["Keys"]["id"]["S"]] = data["SequenceNumber"]
            iterator = page.get("NextShardIterator")
            if set(self.puts) <= result.keys():
                return result if raw else {k: int(v) for k, v in result.items()}
            time.sleep(0.05)
        assert set(self.puts) <= result.keys(), "environment: source stream did not expose harness writes"
        return result if raw else {k: int(v) for k, v in result.items()}

    def launch(self, mode="latest", limit=3, control=None, wait=True,
               endpoint="http://localhost:4566"):
        worker = Worker(self, mode, limit, control, endpoint)
        self.workers.append(worker)
        if wait:
            worker.ready()
        return worker

    def close(self):
        for worker in self.workers:
            worker.cleanup()
        self.db.delete_table(TableName=self.source)
        if self.own_checkpoints:
            self.db.delete_table(TableName=self.checkpoints)


class Worker:
    def __init__(self, feed, mode, limit, control=None, endpoint="http://localhost:4566"):
        self.feed = feed
        self.directory = tempfile.mkdtemp(prefix="feed-role-")
        temporary = Path(self.directory) / "tmp"
        temporary.mkdir()
        self.stderr = open(Path(self.directory) / "stderr", "w+")
        self.stderr_text = None
        env = {**os.environ, "PYTHONPATH": "/app", "TMPDIR": str(temporary),
               "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"}
        if control:
            env["FEED_CONTROL_SOCKET"] = control.address
        self.process = subprocess.Popen(
            [sys.executable, "/app/cdc_reader.py", "--endpoint-url", endpoint,
             "--region", "us-east-1", "--source-table", feed.source,
             "--checkpoint-table", feed.checkpoints, "--start-from", mode,
             "--checkpoint-limit", str(limit)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr,
            text=True, bufsize=1, cwd=self.directory, env=env, start_new_session=True)
        self.events = queue.Queue()
        self.lines = []
        self.delivered = []
        self.errors = []
        self.is_ready = False
        self.auto_ack = False
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()

    def read(self):
        for line in self.process.stdout:
            self.events.put(line.rstrip("\n"))

    def pump(self, duration=0.2):
        until = time.monotonic() + duration
        while time.monotonic() < until:
            try:
                line = self.events.get(timeout=min(0.05, max(0.001, until-time.monotonic())))
            except queue.Empty:
                continue
            self.lines.append(line)
            if line == "READY":
                self.is_ready = True
            elif line.startswith("DELIVER "):
                try:
                    record = json.loads(line[8:])
                    assert isinstance(record["id"], str)
                    assert record["shard"] == self.feed.shard
                    int(record["seq"])
                    self.delivered.append(record)
                    if self.auto_ack:
                        self.ack(record)
                except Exception as error:
                    self.errors.append("bad delivery: " + str(error))
            else:
                self.errors.append("unexpected stdout: " + line)

    def ready(self):
        until = time.monotonic() + 20
        while time.monotonic() < until and not self.is_ready and self.process.poll() is None:
            self.pump(0.05)
        self.pump(0.05)
        assert self.is_ready, "worker never became ready: " + self.diagnostic()

    def wait_for(self, identities, timeout=15, ack=False):
        self.auto_ack = ack
        if ack:
            for record in self.delivered:
                self.ack(record)
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if set(identities) <= {r["id"] for r in self.delivered}:
                self.pump(0.3)
                return
            if self.process.poll() is not None:
                self.pump(0.05)
                return
            self.pump(0.05)

    def ack(self, record):
        try:
            self.process.stdin.write("ACK " + str(record["seq"]) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, ValueError):
            self.errors.append("worker stopped accepting ACKs")

    def kill(self):
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGKILL)
        self.process.wait(timeout=10)
        self.reader.join(timeout=2)
        self.pump(0.1)
        # Ensure the process group's consumers cannot collect downtime records.
        try:
            os.killpg(self.process.pid, 0)
        except ProcessLookupError:
            pass
        else:
            self.errors.append("worker process group survived SIGKILL")
        self.retire_directory()

    def term(self):
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.errors.append("graceful stop exceeded 10 seconds")
            self.kill()
        self.reader.join(timeout=2)
        self.pump(0.1)
        if self.process.returncode != 0:
            self.errors.append("graceful stop status " + str(self.process.returncode))
        self.retire_directory()

    def retire_directory(self):
        if self.stderr_text is None:
            self.stderr.flush()
            self.stderr.seek(0)
            self.stderr_text = self.stderr.read()
            self.stderr.close()
        shutil.rmtree(self.directory, ignore_errors=True)

    def diagnostic(self):
        if self.stderr_text is None:
            self.stderr.flush()
            self.stderr.seek(0)
            stderr = self.stderr.read()
        else:
            stderr = self.stderr_text
        return json.dumps({"lines": self.lines, "errors": self.errors,
                           "stderr": stderr, "status": self.process.poll()})

    def cleanup(self):
        if self.process.poll() is None:
            self.kill()
        self.process.stdin.close()
        self.process.stdout.close()
        self.stderr.close()
        shutil.rmtree(self.directory, ignore_errors=True)


def checkpoint(rows, shard):
    return [int(r["SequenceNumber"]["S"]) for r in rows
            if r["ShardID"]["S"] == shard and "SequenceNumber" in r]


def require_safe(rows, ceiling, failures, label):
    for row in rows:
        if "SequenceNumber" in row:
            value = int(row["SequenceNumber"]["S"])
            if value > ceiling:
                failures.append(f"{label}: checkpoint {value} exceeds acknowledged boundary {ceiling}")


def observe(worker, required, allowed, seqs, failures, label):
    ids = [r["id"] for r in worker.delivered]
    if not set(required) <= set(ids):
        failures.append(f"{label}: missing {set(required)-set(ids)}; got {ids}")
    if not set(ids) <= set(allowed):
        failures.append(f"{label}: unexpected deliveries {set(ids)-set(allowed)}")
    observed = []
    for r in worker.delivered:
        if seqs.get(r["id"]) != int(r["seq"]):
            failures.append(f"{label}: delivery does not identify its source stream record: {r}")
        observed.append(int(r["seq"]))
    if observed != sorted(observed):
        failures.append(f"{label}: deliveries out of stream order")
    failures.extend(f"{label}: {e}" for e in worker.errors)


def test_replacements(aws):
    feed = Feed(aws)
    failures = []
    snapshots = {}
    try:
        limit = 3 + int(feed.token[-1], 16) % 3
        pre = [feed.put("old-" + str(i)) for i in range(3 + int(feed.token[0], 16) % 3)]
        time.sleep(3)
        one = feed.launch(limit=limit)
        snapshots["ready"] = feed.snapshot()
        a = feed.put("a")
        one.wait_for([a])
        one.kill()
        snapshots["first_crash"] = feed.snapshot()
        b = feed.put("b")
        time.sleep(3)
        two = feed.launch(limit=limit)
        two.wait_for([a, b])
        for record in two.delivered:
            if record["id"] == a:
                two.ack(record)
        two.pump(1)
        two.kill()
        snapshots["second_crash"] = feed.snapshot()
        d = feed.put("d")
        time.sleep(3)
        three = feed.launch(limit=limit)
        c = feed.put("c")
        three.wait_for([b, d, c], ack=True)
        three.term()
        snapshots["closed"] = feed.snapshot()
        e = feed.put("e")
        four = feed.launch(limit=limit)
        f = feed.put("f")
        four.wait_for([e, f], ack=True)
        four.term()
        seqs = feed.sequences()
        if not snapshots["ready"]:
            failures.append("READY preceded durable subscription state")
        require_safe(snapshots["ready"], seqs[pre[-1]], failures, "READY")
        require_safe(snapshots["first_crash"], seqs[pre[-1]], failures, "first crash")
        require_safe(snapshots["second_crash"], seqs[a], failures, "second crash")
        if checkpoint(snapshots["closed"], feed.shard) != [seqs[c]]:
            failures.append("graceful stop did not checkpoint the acknowledged contiguous frontier")
        observe(one, [a], [a], seqs, failures, "run 1")
        observe(two, [a, b], [a, b], seqs, failures, "run 2")
        observe(three, [b, d, c], [a, b, d, c], seqs, failures, "run 3")
        observe(four, [e, f], [e, f], seqs, failures, "run 4")
        remaining = feed.db.scan(TableName=feed.source, ConsistentRead=True)["Items"]
        if {r["id"]["S"] for r in remaining} != set(feed.puts):
            failures.append("worker changed source data")
        evidence = {"snapshots": snapshots, "workers": [w.diagnostic() for w in feed.workers]}
        assert not failures, "\n".join(failures) + "\n" + json.dumps(evidence)
    finally:
        feed.close()


def wait_checkpoint(feed, expected, timeout=5):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        rows = feed.snapshot()
        if checkpoint(rows, feed.shard) == [expected]:
            return rows
        time.sleep(0.05)
    return feed.snapshot()


def test_acknowledgements(aws):
    feed = Feed(aws)
    failures = []
    try:
        initial = [feed.put("event-" + str(i)) for i in range(12)]
        seqs = feed.sequences()
        one = feed.launch(mode="oldest", limit=3)
        one.wait_for(initial)
        for record in one.delivered:
            if record["id"] != initial[0]:
                one.ack(record)
                one.ack(record)
        one.pump(0.6)
        require_safe(feed.snapshot(), seqs[initial[0]]-1, failures, "open gap")
        one.term()
        require_safe(feed.snapshot(), seqs[initial[0]]-1, failures, "close with gap")
        two = feed.launch(mode="oldest", limit=3)
        two.wait_for(initial)
        for record in reversed(two.delivered):
            two.ack(record)
        rows = wait_checkpoint(feed, seqs[initial[-1]])
        if checkpoint(rows, feed.shard) != [seqs[initial[-1]]]:
            failures.append("closing the ACK gap did not persist the contiguous frontier")
        extras = [feed.put("tail-" + str(i)) for i in range(2)]
        two.wait_for(initial + extras)
        for record in two.delivered:
            if record["id"] in extras:
                two.ack(record)
                two.ack(record)
        two.pump(0.6)
        if checkpoint(feed.snapshot(), feed.shard) != [seqs[initial[-1]]]:
            failures.append("sub-limit ACKs or repeated ACKs bypassed batching")
        two.term()
        seqs = feed.sequences()
        if checkpoint(feed.snapshot(), feed.shard) != [seqs[extras[-1]]]:
            failures.append("Close did not flush sub-limit acknowledged progress")
        three = feed.launch(mode="oldest", limit=3)
        fresh = feed.put("next")
        three.wait_for([fresh], ack=True)
        three.term()
        seqs = feed.sequences()
        observe(one, initial, initial, seqs, failures, "unsettled")
        observe(two, initial + extras, initial + extras, seqs, failures, "settled")
        observe(three, [fresh], [fresh], seqs, failures, "resumed")
        assert not failures, "\n".join(failures) + "\n" + "\n".join(w.diagnostic() for w in feed.workers)
    finally:
        feed.close()


def test_existing_state(aws):
    feed = Feed(aws)
    other = None
    failures = []
    try:
        old = [feed.put("prior-" + str(i)) for i in range(3)]
        raw = feed.sequences(raw=True)
        seqs = {k: int(v) for k, v in raw.items()}
        feed.db.put_item(TableName=feed.checkpoints, Item={
            "StreamArn": {"S": feed.arn}, "ShardID": {"S": feed.shard},
            "SequenceNumber": {"S": raw[old[1]]}})
        one = feed.launch(limit=1)
        one.wait_for([old[2]], ack=True)
        rows = wait_checkpoint(feed, seqs[old[2]])
        if checkpoint(rows, feed.shard) != [seqs[old[2]]]:
            failures.append("checkpoint-limit 1 did not persist its ACK")
        one.kill()
        two = feed.launch(limit=1)
        live = feed.put("live")
        two.wait_for([live], ack=True)
        two.term()
        seqs = feed.sequences()
        observe(one, [old[2]], [old[2]], seqs, failures, "legacy resume")
        observe(two, [live], [live], seqs, failures, "persisted resume")
        # A different stream's rows in the same table are not this subscription.
        other = Feed(aws, checkpoint_table=feed.checkpoints)
        backlog = [other.put("foreign-old-" + str(i)) for i in range(2)]
        time.sleep(3)
        independent = other.launch(limit=1)
        new = other.put("foreign-live")
        independent.wait_for([new], ack=True)
        independent.term()
        otherseq = other.sequences()
        observe(independent, [new], [new], otherseq, failures, "independent stream")
        assert not failures, "\n".join(failures)
    finally:
        if other:
            other.close()
        feed.close()


class Control:
    """Schedule the existing launcher announcements without reading application state."""
    def __init__(self, stage):
        self.directory = tempfile.mkdtemp(prefix="feed-launcher-")
        self.address = str(Path(self.directory) / "control")
        self.stage = stage
        self.seen = queue.Queue()
        self.resume = threading.Event()
        self.closed = threading.Event()
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(self.address)
        self.listener.listen(8)
        self.listener.settimeout(0.2)
        self.held = False
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while not self.closed.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(5)
                body = b""
                while b"\n" not in body:
                    part = connection.recv(4096)
                    if not part:
                        break
                    body += part
                facts = json.loads(body)
                if facts["stage"] == self.stage and not self.held:
                    self.held = True
                    self.seen.put(facts)
                    self.resume.wait(45)
                try:
                    connection.sendall(b"continue\n")
                except (BrokenPipeError, ConnectionResetError):
                    pass

    def wait(self):
        try:
            return self.seen.get(timeout=20)
        except queue.Empty:
            pytest.fail("launcher did not receive " + self.stage)

    def release(self):
        self.resume.set()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.release()
        self.closed.set()
        self.listener.close()
        self.thread.join(timeout=3)
        shutil.rmtree(self.directory, ignore_errors=True)


def test_overlapping_launches(aws):
    feed = Feed(aws)
    failures = []
    try:
        backlog = [feed.put("backlog-" + str(i)) for i in range(3)]
        time.sleep(3)
        with Control("loaded") as gate:
            slow = feed.launch(control=gate, wait=False)
            gate.wait()
            fast = feed.launch()
            a = feed.put("during-launch")
            fast.wait_for([a])
            fast.kill()
            b = feed.put("between-launches")
            time.sleep(3)
            gate.release()
            slow.ready()
            slow.wait_for([a, b])
            slow.kill()
        c = feed.put("after-overlap")
        time.sleep(3)
        final = feed.launch()
        final.wait_for([a, b, c], ack=True)
        final.term()
        seqs = feed.sequences()
        observe(fast, [a], [a], seqs, failures, "first acquisition")
        observe(slow, [a, b], [a, b], seqs, failures, "delayed acquisition")
        observe(final, [a, b, c], [a, b, c], seqs, failures, "post-overlap recovery")
        assert not failures, "\n".join(failures) + "\n" + "\n".join(w.diagnostic() for w in feed.workers)
    finally:
        feed.close()


class Delay:
    """An HTTP relay in front of the endpoint for one worker. While armed it
    keeps that worker's progress writes to the checkpoint table in flight
    until released, the way a slow network keeps an already sent request."""
    WRITES = ("PutItem", "UpdateItem", "DeleteItem", "BatchWriteItem", "TransactWriteItems")

    def __init__(self, table, upstream=("localhost", 4566)):
        delay = self
        self.table = table.encode()
        self.upstream = upstream
        self.armed = False
        self.lock = threading.Lock()
        self.held = []
        self.opened = threading.Event()

        class Relay(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                delay.relay(self, body)

            def do_GET(self):
                delay.relay(self, b"")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Relay)
        self.server.daemon_threads = True
        self.address = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def relay(self, handler, body):
        target = handler.headers.get("X-Amz-Target", "")
        gate = None
        with self.lock:
            if (self.armed and target.split(".")[-1] in self.WRITES
                    and self.table in body and b"SequenceNumber" in body):
                gate = (threading.Event(), threading.Event())
                self.held.append(gate)
                self.opened.set()
        if gate:
            gate[0].wait(60)
        try:
            upstream = http.client.HTTPConnection(*self.upstream, timeout=30)
            headers = {k: v for k, v in handler.headers.items()
                       if k.lower() not in ("connection", "content-length")}
            upstream.request(handler.command, handler.path, body=body, headers=headers)
            response = upstream.getresponse()
            payload = response.read()
            status = response.status
            reply = [(k, v) for k, v in response.getheaders()
                     if k.lower() not in ("connection", "content-length", "transfer-encoding")]
            upstream.close()
        except OSError:
            handler.close_connection = True
            return
        finally:
            if gate:
                gate[1].set()
        try:
            handler.send_response(status)
            for k, v in reply:
                handler.send_header(k, v)
            handler.send_header("Content-Length", str(len(payload)))
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.wfile.write(payload)
        except OSError:
            pass  # the worker gave up on this request; it reached AWS anyway
        handler.close_connection = True

    def arm(self):
        with self.lock:
            self.armed = True

    def wait_held(self, timeout=5):
        return self.opened.wait(timeout)

    def release(self):
        """Let every held write reach the endpoint, and wait until it has."""
        with self.lock:
            self.armed = False
            held = list(self.held)
        for opened, _ in held:
            opened.set()
        for _, landed in held:
            landed.wait(30)
        return len(held)

    def close(self):
        self.release()
        self.server.shutdown()
        self.server.server_close()


@pytest.mark.parametrize("closing", [False, True], ids=["batch", "close"])
def test_pending_checkpoint_handoff(aws, closing):
    feed = Feed(aws)
    cleanup = []
    failures = []
    snapshots = {}
    try:
        backlog = [feed.put("historical-" + str(i)) for i in range(2)]
        time.sleep(3)
        count = 2 + int(feed.token[0], 16) % 3
        delay = Delay(feed.checkpoints)
        cleanup.append(delay.close)
        with Control("checkpoint") as gate:
            old = feed.launch(limit=count + 5 if closing else count, control=gate,
                              endpoint=delay.address)
            delay.arm()
            events = [feed.put("inflight-" + str(i)) for i in range(count)]
            old.wait_for(events)
            for record in old.delivered:
                old.ack(record)
            if closing:
                old.process.send_signal(signal.SIGTERM)
            gate.wait()
            # The predecessor's write leaves before the takeover and is still
            # in flight when the successor acquires the subscription.
            gate.release()
            delay.wait_held()
            successor = feed.launch(limit=count + 5)
            successor.wait_for(events)
            snapshots["successor_ready"] = feed.snapshot()
            delay.release()
            old.pump(0.8)
            snapshots["predecessor_released"] = feed.snapshot()
            # No ACK has been authorized in the successor's tenure. Even a
            # predecessor write admitted before takeover must not commit now.
            seqs = feed.sequences()
            require_safe(snapshots["predecessor_released"], seqs[backlog[-1]],
                         failures, "superseded checkpoint")
            if closing:
                old.term()
            else:
                old.kill()
            successor.kill()
        downtime = feed.put("handoff-downtime")
        time.sleep(3)
        recovered = feed.launch(limit=count + 5)
        recovered.wait_for(events + [downtime])
        seqs = feed.sequences()
        observe(successor, events, events, seqs, failures, "successor")
        observe(recovered, events + [downtime], events + [downtime], seqs, failures, "recovery")
        # The current owner must retain ordinary sub-limit Close semantics.
        for record in recovered.delivered:
            if record["id"] == events[0]:
                recovered.ack(record)
        recovered.term()
        snapshots["current_close"] = feed.snapshot()
        if checkpoint(snapshots["current_close"], feed.shard) != [seqs[events[0]]]:
            failures.append("current owner lost its sub-limit contiguous flush")
        final = feed.launch()
        final.wait_for(events[1:] + [downtime], ack=True)
        final.term()
        observe(final, events[1:] + [downtime], events[1:] + [downtime], seqs, failures, "final resume")
        assert not failures, "\n".join(failures) + "\n" + json.dumps(snapshots) + "\n" + "\n".join(w.diagnostic() for w in feed.workers)
    finally:
        for close in cleanup:
            close()
        feed.close()


def test_commit_during_successor_startup(aws):
    feed = Feed(aws)
    failures = []
    try:
        old = feed.launch(mode="oldest", limit=2)
        events = [feed.put("before-transfer-" + str(i)) for i in range(2)]
        old.wait_for(events)
        with Control("loaded") as gate:
            successor = feed.launch(mode="oldest", control=gate, wait=False)
            gate.wait()
            for record in old.delivered:
                old.ack(record)
            seqs = feed.sequences()
            rows = wait_checkpoint(feed, seqs[events[-1]])
            committed = checkpoint(rows, feed.shard) == [seqs[events[-1]]]
            if not committed:
                # The launch took the subscription before announcing "loaded",
                # so the predecessor was already superseded and its ACKs commit
                # nothing; the successor then owes every one of those events.
                require_safe(rows, seqs[events[0]] - 1, failures, "superseded commit")
            gate.release()
            successor.ready()
            tail = feed.put("after-transfer")
            successor.wait_for([tail] if committed else events + [tail], ack=True)
            successor.term()
        old.kill()
        seqs = feed.sequences()
        if committed:
            observe(successor, [tail], [tail], seqs, failures, "resume after pre-transfer commit")
        else:
            observe(successor, events + [tail], events + [tail], seqs, failures,
                    "resume after fenced predecessor")
        assert not failures, "\n".join(failures) + "\n" + successor.diagnostic()
    finally:
        feed.close()
