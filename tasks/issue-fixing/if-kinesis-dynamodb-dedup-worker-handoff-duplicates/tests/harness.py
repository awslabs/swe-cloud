"""Replacement drill harness: controller, append service, fixtures, readers.

Nothing here is part of the shipped image. The controller provisions the
streams and the state table for a trial, drives the worker processes through
the control protocol, injects the replacement, and records what the output
stream and the state table ended up holding.
"""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, ConnectionError as ServiceUnreachable

SHARD = "shardId-000000000000"
WORKER = "/app/worker.py"
DETAILS_PATH = "/logs/verifier/details.txt"
ACCEPTED_HOLD_SECONDS = 120.0

RETRYABLE = {
    "ProvisionedThroughputExceededException",
    "ExpiredIteratorException",
    "ThrottlingException",
    "InternalServerError",
}

PARAMS = {
    "warmup_unique": 3,
    "target_unique": 1,
    "tail_unique": 2,
    "copies_per_id": 2,
    "expected_unique": 6,
    "get_records_limit": 2,
    "poll_interval_ms": 250,
    "hook_wait_seconds": 20,
    "kill_signal": signal.SIGKILL,
    "join_timeout_seconds": 5,
    "adapter_drain_timeout_seconds": 5,
    "accepted_release_seconds": 26.0,  # late, but inside the 30 s recovery bound
    "recovery_deadline_seconds": 30,
    "completion_deadline_seconds": 45,
    "quiet_observation_seconds": 2,
    "verification_deadline_seconds": 15,
    "api_timeout_seconds": 2,
    "sdk_total_attempts": 1,
    "initial_epoch": 1,
    "replacement_epoch": 2,
}


def detail(line):
    try:
        os.makedirs(os.path.dirname(DETAILS_PATH), exist_ok=True)
        with open(DETAILS_PATH, "a", encoding="utf-8") as handle:
            handle.write("%s\n" % line)
    except OSError:
        pass


class HarnessFailure(Exception):
    def __init__(self, phase, message, fixture=False):
        super().__init__(message)
        self.phase = phase
        self.fixture = fixture


def aws_config(params):
    """The fixture's own control-plane budget.

    `api_timeout_seconds` is the budget the *worker* works under, not the
    budget the drill provisions streams and tables with: the emulator lazily
    starts a service on its first call and that first call can take seconds,
    which must not be read as anything about the worker.
    """
    budget = max(30, int(params["api_timeout_seconds"]))
    return Config(
        retries={"total_max_attempts": int(params["sdk_total_attempts"])},
        connect_timeout=min(10, budget),
        read_timeout=budget,
    )


def client(service, endpoint, region="us-east-1"):
    return boto3.client(
        service,
        endpoint_url=endpoint,
        region_name=region,
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "test"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "test"),
        config=aws_config(PARAMS),
    )


def warm(endpoint, timeout=120):
    """Touch both data planes until they answer, before anything is timed."""
    deadline = time.monotonic() + timeout
    pending = {"kinesis": "list_streams", "dynamodb": "list_tables"}
    while pending and time.monotonic() < deadline:
        for service in list(pending):
            try:
                getattr(client(service, endpoint), pending[service])()
                pending.pop(service)
            except Exception:
                time.sleep(1.0)
    return not pending


def retryable(call, sleep=0.2, attempts=8, safe=False):
    """Retry a call the emulator may answer late or busily.

    `safe` marks a call a retry cannot double: a read, or a write whose
    repetition lands on the same value. Appends are never retried here, since
    a retry that reached the stream would put a second record in it.
    """
    for index in range(attempts):
        try:
            return call()
        except ClientError as error:
            code = error.response["Error"]["Code"]
            if code in RETRYABLE and index + 1 < attempts:
                time.sleep(sleep)
                continue
            raise
        except ServiceUnreachable:
            if safe and index + 1 < attempts:
                time.sleep(sleep)
                continue
            raise
    raise RuntimeError("unreachable")


class ReadGate:
    """One GetRecords call per shard per interval, across all verifier readers."""

    def __init__(self, interval_seconds):
        self._interval = interval_seconds
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self._interval
            delay = start - now
        if delay > 0:
            time.sleep(delay)


class AppendService:
    """The only legal write path to the output stream during a trial."""

    def __init__(self, kinesis, stream, partition_key, params):
        self._kinesis = kinesis
        self._stream = stream
        self._partition_key = partition_key
        self._params = params
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._last_sequence = None
        self._trace = []
        self._inflight = 0
        self._hold_armed = False
        self._defer_armed = False
        self._committed = None
        self._release = None
        self._server = None
        self._thread = None

    # -- lifecycle --------------------------------------------------------

    def start(self):
        handler = type("Handler", (ServiceHandler,), {"service": self})
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self._server.server_address[1]

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    # -- writing ----------------------------------------------------------

    def _put(self, record):
        """Append one record. The write lock serializes appends and this
        service is the only writer the output stream has, so the order the
        records arrive in is the order the stream holds them in."""
        return retryable(
            lambda: self._kinesis.put_record(
                StreamName=self._stream,
                Data=json.dumps(record).encode("utf-8"),
                PartitionKey=self._partition_key,
            )
        )["SequenceNumber"]

    def publish(self, request):
        record = {
            "kind": "DATA",
            "run_id": request.get("run_id"),
            "partition_id": request.get("partition_id"),
            "epoch": request.get("epoch"),
            "input_sequence": request.get("input_sequence"),
            "messageId": request.get("messageId"),
            "payload": request.get("payload"),
        }
        with self._state_lock:
            self._inflight += 1
            defer = self._defer_armed
            self._defer_armed = False
            accepted = self._committed
            release = self._release
        try:
            if defer and accepted is not None and release is not None:
                # The request is accepted and its record is not written yet: it
                # reaches the stream when the hold lifts, not when the request
                # arrives, and the answer follows the record.
                accepted.set()
                release.wait(timeout=ACCEPTED_HOLD_SECONDS)
            with self._write_lock:
                sequence = self._put(record)
                with self._state_lock:
                    self._trace.append(
                        {"sequence": sequence, "record": record, "pid": os.getpid()}
                    )
                    hold = self._hold_armed
                    committed = self._committed
                    answer_release = self._release
                    if hold:
                        self._hold_armed = False
            if hold and committed is not None and answer_release is not None:
                committed.set()
                answer_release.wait(timeout=ACCEPTED_HOLD_SECONDS)
        finally:
            with self._state_lock:
                self._inflight -= 1
        return {"status": "appended", "sequence": sequence}

    def append_barrier(self, barrier_id):
        record = {
            "kind": "BARRIER",
            "run_id": self._run_id,
            "partition_id": self._partition_key,
            "barrier_id": barrier_id,
        }
        with self._write_lock:
            sequence = self._put(record)
            with self._state_lock:
                self._trace.append({"sequence": sequence, "record": record})
        return {"barrier_id": barrier_id, "sequence": sequence}

    def last_boundary(self):
        with self._state_lock:
            for entry in reversed(self._trace):
                if entry["record"].get("kind") == "BARRIER":
                    return {
                        "status": "boundary",
                        "barrier_id": entry["record"].get("barrier_id"),
                        "sequence": entry["sequence"],
                    }
        return {"status": "none"}

    # -- control ----------------------------------------------------------

    def bind(self, run_id, partition_id):
        self._run_id = run_id
        self._partition_key = partition_id

    def arm_hold(self):
        with self._state_lock:
            self._hold_armed = True
            self._committed = threading.Event()
            self._release = threading.Event()

    def arm_defer(self):
        """Hold the next accepted request before its record is written."""
        with self._state_lock:
            self._defer_armed = True
            self._committed = threading.Event()
            self._release = threading.Event()

    def pending(self):
        """Requests the service has accepted and not yet answered."""
        with self._state_lock:
            return self._inflight

    def wait_committed(self, timeout):
        event = self._committed
        if event is None:
            return False
        return event.wait(timeout)

    def release(self):
        with self._state_lock:
            event = self._release
        if event is not None:
            event.set()

    def drain(self, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._state_lock:
                if self._inflight == 0:
                    return True
            time.sleep(0.05)
        with self._state_lock:
            return self._inflight == 0

    def trace(self):
        with self._state_lock:
            return list(self._trace)

    def appended_message_ids(self):
        with self._state_lock:
            return [
                entry["record"].get("messageId")
                for entry in self._trace
                if entry["record"].get("kind") == "DATA"
            ]


class ServiceHandler(BaseHTTPRequestHandler):
    service = None
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _reply(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        try:
            request = json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            self._reply(400, {"status": "malformed"})
            return
        if self.path == "/publish":
            try:
                answer = self.service.publish(request)
            except Exception as error:
                self._reply(502, {"status": "failed", "error": str(error)})
                return
            self._reply(200, answer)
            return
        if self.path == "/admin/barrier":
            self._reply(200, self.service.append_barrier(request.get("barrier_id") or uuid.uuid4().hex))
            return
        if self.path == "/admin/arm-hold":
            self.service.arm_hold()
            self._reply(200, {"status": "armed"})
            return
        if self.path == "/admin/arm-defer":
            self.service.arm_defer()
            self._reply(200, {"status": "armed"})
            return
        if self.path == "/admin/release":
            self.service.release()
            self._reply(200, {"status": "released"})
            return
        self._reply(404, {"status": "unknown", "path": self.path})

    def do_GET(self):
        if self.path == "/recovery-boundary":
            self._reply(200, self.service.last_boundary())
            return
        if self.path == "/pending":
            self._reply(200, {"status": "ok", "accepted": self.service.pending()})
            return
        if self.path == "/admin/trace":
            self._reply(200, {"trace": self.service.trace()})
            return
        if self.path == "/admin/drain":
            settled = self.service.drain(float(self.headers.get("X-Timeout", "5")))
            self._reply(200, {"status": "drained" if settled else "busy"})
            return
        self._reply(404, {"status": "unknown", "path": self.path})


class WorkerProcess:
    """One worker subprocess, driven over the control protocol."""

    def __init__(self, worker_id, config_path, params):
        self.worker_id = worker_id
        self.events = []
        self.stderr_tail = []
        self._queue = queue.Queue()
        self._lock = threading.Lock()
        self._hold = False
        self.process = subprocess.Popen(
            [sys.executable, WORKER, "--config", config_path, "--worker-id", worker_id],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=tempfile.mkdtemp(prefix="worker-%s-" % worker_id),
            env=dict(
                os.environ,
                PYTHONPATH="/app",
                AWS_ENDPOINT_URL=os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566"),
                AWS_DEFAULT_REGION="us-east-1",
                AWS_ACCESS_KEY_ID="test",
                AWS_SECRET_ACCESS_KEY="test",
            ),
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self):
        try:
            for line in self.process.stdout:
                text = line.strip()
                if not text:
                    continue
                try:
                    message = json.loads(text)
                except ValueError:
                    continue
                self.events.append(message)
                self._queue.put(message)
                if message.get("event") == "AFTER_SEEN_BEFORE_PUBLISH":
                    with self._lock:
                        hold = self._hold
                    if not hold:
                        self.send({"cmd": "CONTINUE"})
        except Exception:
            pass
        finally:
            self._queue.put(None)

    def _read_stderr(self):
        try:
            for line in self.process.stderr:
                self.stderr_tail.append(line.rstrip("\n"))
                del self.stderr_tail[:-60]
        except Exception:
            pass

    def arm_hold(self):
        with self._lock:
            self._hold = True

    def send(self, command):
        try:
            self.process.stdin.write(json.dumps(command) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass

    def wait_event(self, name, timeout, match=None):
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
            if message.get("event") != name:
                continue
            if match and any(message.get(key) != value for key, value in match.items()):
                continue
            return message

    def kill(self):
        try:
            os.kill(self.process.pid, PARAMS["kill_signal"])
        except (ProcessLookupError, PermissionError):
            pass

    def reap(self, timeout):
        try:
            self.process.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            return False

    def close(self):
        try:
            self.process.stdin.close()
        except Exception:
            pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)


def read_output(kinesis, stream, gate, limit, deadline, boundary_id=None):
    """Read the output shard from the start; stop after `boundary_id` if given."""
    iterator = retryable(
        lambda: kinesis.get_shard_iterator(
            StreamName=stream, ShardId=SHARD, ShardIteratorType="TRIM_HORIZON"
        ),
        safe=True,
    )["ShardIterator"]
    records = []
    while True:
        if time.monotonic() >= deadline:
            raise HarnessFailure(
                "scan", "output read did not reach %r before its deadline" % boundary_id
            )
        gate.wait()
        response = retryable(
            lambda: kinesis.get_records(ShardIterator=iterator, Limit=limit), safe=True
        )
        iterator = response.get("NextShardIterator", iterator)
        for record in response.get("Records", []):
            payload = json.loads(record["Data"].decode("utf-8"))
            records.append({"sequence": record["SequenceNumber"], **payload})
            if (
                boundary_id is not None
                and payload.get("kind") == "BARRIER"
                and payload.get("barrier_id") == boundary_id
            ):
                return records
