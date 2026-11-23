"""Verifier harness: fixtures, destination receiver, control plane, phase drivers.

Everything here is grader-owned. The service under test is started as a plain
local process with PYTHONPATH=/app and its own working directory; the only
state shared with it is the emulated AWS services.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from cdk_seed.cfn_deploy import deploy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import boto3

APP_DIR = "/app"
WORKER = os.path.join(APP_DIR, "dispatcher.py")
DECISION_PATH = os.path.join(APP_DIR, "decision.json")

P = {
    "delivery_batch_size": 4,
    "healthy_event_count": 24,
    "failure_event_count": 8,
    "head_sequence": 1,
    "ack_delay_ms": 500,
    "http_timeout_seconds": 2.0,
    "visibility_seconds": 8,
    "healthy_deadline_seconds": 60.0,
    "recovery_deadline_seconds": 40.0,
    "barrier_seconds": 5.0,
    "retry_backoff_seconds": 1.0,
    "poll_wait_seconds": 1,
    "outage_observation_seconds": 8.0,
    "disposition_deadline_seconds": 5.0,
    "inspection_wait_seconds": 4.0,
    # An inspection may have to wait out a whole visibility window before the
    # head can be taken at all: a service that leaves a rejected receipt in
    # flight instead of releasing it is within its rights, and so is the
    # stopped process whose receive is still running out.
    "inspection_deadline_seconds": 10.0,
    "inspection_batch_size": 1,
    "release_visibility_seconds": 0,
    "queue_delay_seconds": 0,
    "queue_retention_seconds": 300,
    "sqs_poll_wait_seconds": 1,
    "success_status": 200,
    "failure_status": 503,
    # How long the control plane sits on the first report of an accepted
    # delivery before answering it. The contract only promises an answer
    # eventually; the service has to live with the delay.
    "held_success_report_seconds": 10.0,
    "case_deadline_seconds": 180.0,
}

QUEUE_REGISTRY: list = []  # (queue_url, run_id) seen by this verifier run


def now_ms() -> int:
    return int(time.time() * 1000)


def s(value) -> dict:
    return {"S": str(value)}


def num(value) -> dict:
    return {"N": str(value)}


def boolean(value) -> dict:
    return {"BOOL": bool(value)}


def aws_client(service: str):
    return boto3.client(
        service,
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566"),
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "test"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "test"),
    )


# --------------------------------------------------------------------------- #
# destination receiver
# --------------------------------------------------------------------------- #


class ReceiverState:
    def __init__(self, ddb, table, run_id, endpoint_id, batch_limit, ack_delay_ms, blocked_seq):
        self.ddb = ddb
        self.table = table
        self.run_id = run_id
        self.endpoint_id = endpoint_id
        self.batch_limit = batch_limit
        self.ack_delay_ms = ack_delay_ms
        self.blocked_seq = blocked_seq
        self.event_count = 0
        self.lock = threading.Lock()
        self.head_rejections = 0
        self.requests_seen = 0

    def next_ordinal(self) -> int:
        answer = self.ddb.update_item(
            TableName=self.table,
            Key={"pk": s(self.run_id), "sk": s("CTR#REQ")},
            UpdateExpression="ADD #o :one",
            ExpressionAttributeNames={"#o": "ordinal"},
            ExpressionAttributeValues={":one": num(1)},
            ReturnValues="UPDATED_NEW",
        )
        return int(answer["Attributes"]["ordinal"]["N"])

    def poison_row(self) -> dict:
        answer = self.ddb.get_item(
            TableName=self.table,
            Key={"pk": s(self.run_id), "sk": s("CTL#POISON")},
            ConsistentRead=True,
        )
        return answer.get("Item", {})

    def record(
        self,
        ordinal: int,
        request_id: str,
        endpoint: str,
        seqs,
        status_code: int,
        committed: bool,
        contains_blocked: bool,
    ) -> None:
        self.ddb.update_item(
            TableName=self.table,
            Key={"pk": s(self.run_id), "sk": s("REQ#%06d" % ordinal)},
            UpdateExpression=(
                "SET endpoint = :e, seqs = :q, status_code = :c, committed = :m, "
                "contains_blocked = :b, request_id = :r, decided_ms = :d"
            ),
            ExpressionAttributeValues={
                ":e": s(endpoint),
                ":q": s(",".join(str(x) for x in seqs)),
                ":c": num(status_code),
                ":m": boolean(committed),
                ":b": boolean(contains_blocked),
                ":r": s(request_id),
                ":d": num(now_ms()),
            },
        )


def receiver_handler_factory(state: ReceiverState):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _reply(self, status: int, payload: dict):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            try:
                self._handle()
            except Exception as error:  # keep the receiver alive no matter what
                sys.stderr.write("receiver error: %r\n" % (error,))
                try:
                    self._reply(500, {"error": repr(error)})
                except Exception:
                    pass

        def _handle(self):
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b""
            endpoint = self.path.rsplit("/deliver/", 1)[-1].strip("/")
            request_id = uuid.uuid4().hex[:12]
            try:
                body = json.loads(raw or b"{}")
                events = body.get("events")
                assert isinstance(events, list) and events
                seqs = [int(event["seq"]) for event in events]
            except Exception:
                self._reply(400, {"request_id": request_id, "error": "malformed delivery"})
                return
            with state.lock:
                state.requests_seen += 1
            ordinal = state.next_ordinal()
            state.ddb.put_item(
                TableName=state.table,
                Item={
                    "pk": s(state.run_id),
                    "sk": s("REQ#%06d" % ordinal),
                    "arrival_ms": num(now_ms()),
                    "status_code": num(0),
                    "endpoint": s(endpoint),
                    "seqs": s(",".join(str(x) for x in seqs)),
                },
            )
            known = isinstance(seqs, list) and all(
                1 <= x <= state.event_count for x in seqs
            )
            right_size = 1 <= len(seqs) <= state.batch_limit
            poison = state.poison_row()
            blocked = poison.get("enabled", {}).get("BOOL", False)
            blocked_seq = int(poison.get("blocked_seq", {}).get("N", state.blocked_seq))
            contains_blocked = blocked and blocked_seq in seqs
            status = (
                P["success_status"]
                if (right_size and known and not contains_blocked)
                else P["failure_status"]
            )
            if not (right_size and known):
                status = 400
            if status == P["success_status"]:
                # an accepting destination takes the delivery seriously before
                # answering; a rejecting one just fails
                time.sleep(state.ack_delay_ms / 1000.0)
            committed = status == P["success_status"]
            state.record(ordinal, request_id, endpoint, seqs, status, committed, contains_blocked)
            if contains_blocked:
                with state.lock:
                    state.head_rejections += 1
            self._reply(status, {"request_id": request_id})

    return Handler


# --------------------------------------------------------------------------- #
# control plane
# --------------------------------------------------------------------------- #


class ControlState:
    """Platform control plane for one phase.

    Modes: "plain" (everything CONTINUE/OK, with the first report of accepted
    work answered late), "live" (arm an observation window at the first
    reported rejection, park the next disposition or completion announcement
    once the window closes), "crash" (stop answering at the first reported
    rejection and let the driver kill the process; the replacement starts
    while the destination still rejects, and a completion report that arrives
    before the destination is re-opened is held, not endorsed).
    """

    def __init__(self, mode: str, receiver: ReceiverState, params: dict):
        self.mode = mode
        self.receiver = receiver
        self.params = params
        self.lock = threading.Lock()
        self.ready_at = None
        self.go = False
        self.calls = []
        self.last_call_at = time.monotonic()
        self.window_start = None
        self.window_close = None
        self.parked = []
        self.released = False
        self.reloaded_at = None
        self.done_seen = False
        self.done_at = None
        self.stop_requested = threading.Event()
        self.worker = None  # set by the driver
        self.destination_open = False
        self.done_before_open = False
        self.freeze_pending = False
        self.success_reports = 0
        self.held_success_report = False

    def note(self, kind: str, payload: dict) -> None:
        with self.lock:
            self.calls.append(
                {
                    "at": time.monotonic(),
                    "kind": kind,
                    "status": payload.get("status"),
                    "seqs": payload.get("seqs"),
                    "request_id": payload.get("request_id"),
                }
            )
            self.last_call_at = time.monotonic()

    def head_rejected(self) -> bool:
        with self.receiver.lock:
            return self.receiver.head_rejections > 0

    def decide(self, kind: str, payload: dict):
        """Returns (action, park). park=True means the caller must not answer
        until the driver releases it; park=<seconds> means the control plane
        answers itself once that long has passed."""
        with self.lock:
            now = time.monotonic()
            if kind == "done":
                self.done_seen = True
                self.done_at = now
            if self.mode == "crash":
                if (
                    kind == "after-response"
                    and payload.get("status") == self.params["failure_status"]
                    and not self.stop_requested.is_set()
                ):
                    self.stop_requested.set()
                    return "STOP", True
                if kind == "after-disposition" and self.freeze_pending:
                    # hold the service where it stands so its stream can be
                    # looked at
                    self.freeze_pending = False
                    return "HOLD", True
                if kind == "done":
                    if not self.destination_open:
                        # the destination is still rejecting its head, so the
                        # run's work is not done; the report is held until the
                        # stream has been looked at
                        self.done_before_open = True
                        return "RELOAD", True
                    return "OK", False
                return "CONTINUE", False
            if self.mode == "live":
                if (
                    kind == "after-response"
                    and self.window_start is None
                    and payload.get("status") == self.params["failure_status"]
                    and self.head_rejected()
                ):
                    self.window_start = now
                    self.window_close = now + self.params["outage_observation_seconds"]
                if self.window_start is not None:
                    if kind == "done" and not self.released:
                        # a service that calls it finished while its destination
                        # still rejects work is held here until it has been
                        # looked at
                        return "RELOAD", True
                    if kind == "after-disposition":
                        if self.released:
                            return "CONTINUE", False
                        if now >= self.window_close:
                            return "RELOAD", True
                if kind == "done":
                    return "OK", False
                return "CONTINUE", False
            if self.mode == "plain":
                if (
                    kind == "after-response"
                    and payload.get("status") == self.params["success_status"]
                ):
                    self.success_reports += 1
                    if (
                        self.success_reports == 1
                        and not self.held_success_report
                        and self.params.get("held_success_report_seconds", 0) > 0
                    ):
                        self.held_success_report = True
                        # the control plane takes its time before answering a
                        # report of accepted work
                        return "CONTINUE", float(
                            self.params["held_success_report_seconds"]
                        )
                return ("OK" if kind == "done" else "CONTINUE"), False
            return ("OK" if kind == "done" else "CONTINUE"), False


def control_handler_factory(state: ControlState):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _reply(self, payload: dict):
            body = json.dumps(payload).encode("utf-8")
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            if self.path.startswith("/go") and state.go:
                self._reply({"go": True})
                return
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b""
            payload = {}
            try:
                payload = json.loads(raw or b"{}")
            except Exception:
                payload = {}
            kind = self.path.split("?")[0].strip("/")
            state.note(kind, payload)
            if kind == "ready":
                state.ready_at = time.monotonic()
                self._reply({"ok": True})
                return
            action, park = state.decide(kind, payload)
            if park:
                released = threading.Event()
                slot = {"action": action, "event": released, "kind": kind}
                with state.lock:
                    state.parked.append(slot)
                if isinstance(park, float):
                    hold = threading.Timer(
                        park,
                        lambda: (
                            slot.__setitem__("action", "CONTINUE"),
                            released.set(),
                        ),
                    )
                    hold.daemon = True
                    hold.start()
                released.wait(timeout=300.0)
                with state.lock:
                    action = slot.get("action", action)
                if not released.is_set():
                    return  # the run is over; drop the connection
            self._reply({"action": action})

    return Handler


# --------------------------------------------------------------------------- #
# fixture + phase driver
# --------------------------------------------------------------------------- #


class Phase:
    def __init__(self, kind: str):
        self.kind = kind
        self.run_id = "run-%s" % uuid.uuid4().hex[:10]
        self.token = uuid.uuid4().hex[:8]
        self.endpoint_id = "dst-%s" % self.token
        self.failures = []
        self.report = []
        self.sqs = aws_client("sqs")
        self.ddb = aws_client("dynamodb")
        self.workers = []
        self.deadline = time.monotonic() + P["case_deadline_seconds"]

    # -- setup -------------------------------------------------------------

    def setup(self, event_count: int, blocked_seq=None) -> None:
        self.queue_name = "pending-%s.fifo" % self.token
        self.queue_url = deploy(
            aws_client("cloudformation"), "webhook-test-seed-" + self.token,
            Path(__file__).parent / "cdk_seed/seed.template.json",
            QueueName=self.queue_name, TableName="obs-%s" % self.token,
        )["QueueUrl"]
        QUEUE_REGISTRY.append(self.queue_url)
        self.table = "obs-%s" % self.token
        waiter = self.ddb.get_waiter("table_exists")
        waiter.wait(TableName=self.table, WaiterConfig={"Delay": 1, "MaxAttempts": 20})
        self.ddb.put_item(
            TableName=self.table,
            Item={
                "pk": s(self.run_id),
                "sk": s("CTL#POISON"),
                "enabled": boolean(blocked_seq is not None),
                "blocked_seq": num(blocked_seq if blocked_seq is not None else 0),
            },
        )
        self.blocked_seq = blocked_seq
        self.seeds = {}
        for seq in range(1, event_count + 1):
            nonce = uuid.uuid4().hex
            event = {
                "event_id": "evt-%s-%04d" % (self.token, seq),
                "seq": seq,
                "payload_nonce": nonce,
                "payload": {"seq": seq, "nonce": nonce},
            }
            body = json.dumps(event, separators=(",", ":"), sort_keys=True)
            message_id = self.sqs.send_message(
                QueueUrl=self.queue_url,
                MessageBody=body,
                MessageGroupId=self.endpoint_id,
                MessageDeduplicationId="%s-%04d" % (self.token, seq),
            )["MessageId"]
            self.seeds[seq] = {
                "event_id": event["event_id"],
                "nonce": nonce,
                "body": body,
                "message_id": message_id,
            }
            self.ddb.put_item(
                TableName=self.table,
                Item={
                    "pk": s(self.run_id),
                    "sk": s("SEED#%04d" % seq),
                    "seq": num(seq),
                    "event_id": s(event["event_id"]),
                    "nonce": s(nonce),
                    "message_id": s(message_id),
                    "body": s(body),
                },
                ConditionExpression="attribute_not_exists(pk)",
            )
        # a second write of the same key must be refused; the ledger has one row
        # per accepted event
        try:
            self.ddb.put_item(
                TableName=self.table,
                Item={
                    "pk": s(self.run_id),
                    "sk": s("SEED#%04d" % 1),
                    "seq": num(1),
                    "event_id": s(self.seeds[1]["event_id"]),
                },
                ConditionExpression="attribute_not_exists(pk)",
            )
            self.failures.append("ledger: a duplicate seed row was accepted")
        except Exception:
            pass
        self.event_count = event_count

        self.receiver_state = ReceiverState(
            self.ddb,
            self.table,
            self.run_id,
            self.endpoint_id,
            P["delivery_batch_size"],
            P["ack_delay_ms"],
            blocked_seq or 0,
        )
        self.receiver_state.event_count = event_count
        self.receiver = ThreadingHTTPServer(
            ("127.0.0.1", 0), receiver_handler_factory(self.receiver_state)
        )
        threading.Thread(target=self.receiver.serve_forever, daemon=True).start()
        self.receiver_url = "http://127.0.0.1:%d" % self.receiver.server_address[1]
        self.control_state = ControlState(self.kind, self.receiver_state, P)
        self.control = ThreadingHTTPServer(
            ("127.0.0.1", 0), control_handler_factory(self.control_state)
        )
        threading.Thread(target=self.control.serve_forever, daemon=True).start()
        self.control_url = "http://127.0.0.1:%d" % self.control.server_address[1]
        self.workdir = tempfile.mkdtemp(prefix="phase-%s-" % self.kind)
        self.go_released_at = None

    def teardown(self) -> None:
        if getattr(self, "control_state", None) is not None:
            with self.control_state.lock:
                self.control_state.released = True
                for slot in self.control_state.parked:
                    slot["action"] = "CONTINUE"
                    slot["event"].set()
        for worker in self.workers:
            if worker.poll() is None:
                worker.terminate()
                try:
                    worker.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    worker.kill()
                    worker.wait()
        for server in (getattr(self, "control", None), getattr(self, "receiver", None)):
            if server:
                server.shutdown()
                server.server_close()
        if getattr(self, "workdir", None):
            shutil.rmtree(self.workdir, ignore_errors=True)

    # -- worker ------------------------------------------------------------

    def start_worker(self, phase_name: str):
        config_path = os.path.join(self.workdir, "config-%s.json" % phase_name)
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "run_id": self.run_id,
                    "phase": phase_name,
                    "endpoint_id": self.endpoint_id,
                    "queue_url": self.queue_url,
                    "receiver_url": self.receiver_url,
                    "control_url": self.control_url,
                    "delivery_batch_size": P["delivery_batch_size"],
                    "http_timeout_seconds": P["http_timeout_seconds"],
                    "poll_wait_seconds": P["poll_wait_seconds"],
                    "retry_backoff_seconds": P["retry_backoff_seconds"],
                    "visibility_seconds": P["visibility_seconds"],
                    "barrier_seconds": P["barrier_seconds"],
                    "aws": {
                        "endpoint_url": os.environ.get(
                            "AWS_ENDPOINT_URL", "http://localhost:4566"
                        ),
                        "region_name": os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
                        "aws_access_key_id": os.environ.get("AWS_ACCESS_KEY_ID", "test"),
                        "aws_secret_access_key": os.environ.get(
                            "AWS_SECRET_ACCESS_KEY", "test"
                        ),
                    },
                },
                handle,
            )
        worker = subprocess.Popen(
            [sys.executable, WORKER, "--config", config_path],
            cwd=self.workdir,
            env=dict(os.environ, PYTHONPATH=APP_DIR),
            stdout=subprocess.DEVNULL,
            stderr=open(os.path.join(self.workdir, "worker-%s.log" % phase_name), "wb"),
        )
        self.workers.append(worker)
        return worker

    def barrier(self, worker, label: str) -> bool:
        """Wait for the ready announcement, then release the run."""
        limit = time.monotonic() + 15.0
        while time.monotonic() < limit:
            if self.control_state.ready_at is not None:
                self.control_state.go = True
                self.go_released_at = time.monotonic()
                return True
            if worker.poll() is not None:
                self.failures.append(
                    "%s: service exited before announcing ready (rc=%s); log: %s"
                    % (label, worker.returncode, self.worker_log_tail(8) or ["<silent>"])
                )
                return False
            time.sleep(0.1)
        self.failures.append("%s: no ready announcement within the barrier" % label)
        return False

    def worker_log_tail(self, limit: int = 25) -> list:
        lines = []
        for path in sorted(os.listdir(self.workdir)):
            if path.startswith("worker-") and path.endswith(".log"):
                with open(os.path.join(self.workdir, path), "r", errors="replace") as handle:
                    lines.extend(handle.read().splitlines()[-limit:])
        return lines

    # -- observation points -------------------------------------------------

    def wait_for(self, predicate, timeout: float, label: str):
        limit = time.monotonic() + timeout
        while time.monotonic() < limit:
            if predicate():
                return True
            if all(worker.poll() is not None for worker in self.workers):
                return predicate()
            time.sleep(0.1)
        return predicate()

    def wait_parked(self) -> bool:
        """Wait until the observation window has closed, the service is caught
        at a disposition or completion call, and it has been quiet for the
        inspection wait."""
        params = P
        limit = time.monotonic() + (
            params["outage_observation_seconds"]
            + params["disposition_deadline_seconds"]
            + params["inspection_wait_seconds"]
            + 10.0
        )
        while time.monotonic() < limit:
            with self.control_state.lock:
                parked = bool(self.control_state.parked)
                quiet = time.monotonic() - self.control_state.last_call_at
            window_over = (
                self.control_state.window_close is None
                or time.monotonic() >= self.control_state.window_close
            )
            if parked and quiet >= params["inspection_wait_seconds"] and window_over:
                return True
            time.sleep(0.1)
        return False

    def kill_worker(self, worker, sig=signal.SIGKILL) -> None:
        if worker.poll() is None:
            worker.send_signal(sig)
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait()

    def inspect_head(self, tag: str, deadline_seconds: float = None) -> dict:
        """Look for the head of the stream and compare it with what was
        accepted. One record at a time, each one given straight back."""
        result = {
            "tag": tag,
            "found": False,
            "matches": False,
            "note": "",
            "seen": [],
        }
        seed = self.seeds[P["head_sequence"]]
        result["expected_message_id"] = seed["message_id"]
        seen_ids = set()
        deadline = time.monotonic() + (
            deadline_seconds
            if deadline_seconds is not None
            else P["inspection_deadline_seconds"]
        )
        while time.monotonic() < deadline and len(seen_ids) < 12:
            batch = self.sqs.receive_message(
                QueueUrl=self.queue_url,
                MaxNumberOfMessages=P["inspection_batch_size"],
                WaitTimeSeconds=0,
            ).get("Messages", [])
            if not batch:
                time.sleep(0.1)
                continue
            message = batch[0]
            self.sqs.change_message_visibility(
                QueueUrl=self.queue_url,
                ReceiptHandle=message["ReceiptHandle"],
                VisibilityTimeout=P["release_visibility_seconds"],
            )
            if message["MessageId"] in seen_ids:
                time.sleep(0.1)
                continue
            seen_ids.add(message["MessageId"])
            try:
                seq = int(json.loads(message["Body"])["seq"])
            except Exception:
                seq = None
            result["seen"].append(seq)
            if not result["found"]:
                result["found"] = True
                result["message_id"] = message["MessageId"]
                result["seq"] = seq
            if seq == P["head_sequence"]:
                result["message_id"] = message["MessageId"]
                result["seq"] = seq
                result["body"] = message["Body"]
                if message["MessageId"] != seed["message_id"]:
                    result["note"] = "seq %s is present under a different stream record" % seq
                elif message["Body"] != seed["body"]:
                    result["note"] = "seq %s body differs from what was accepted" % seq
                else:
                    result["matches"] = True
                break
        if not result["found"]:
            result["note"] = "nothing could be taken from the stream for inspection"
        elif not result["matches"] and not result["note"]:
            result["note"] = "seq %s never came back from the stream; saw %s" % (
                P["head_sequence"],
                result["seen"],
            )
        self.ddb.put_item(
            TableName=self.table,
            Item={
                "pk": s(self.run_id),
                "sk": s("INSPECT#%s" % tag),
                "found": boolean(result["found"]),
                "matches": boolean(result["matches"]),
                "message_id": s(result.get("message_id", "")),
                "expected_message_id": s(result.get("expected_message_id", "")),
                "seq": num(result.get("seq") or 0),
                "note": s(result.get("note", "")),
            },
        )
        return result

    def open_destination(self) -> None:
        self.ddb.update_item(
            TableName=self.table,
            Key={"pk": s(self.run_id), "sk": s("CTL#POISON")},
            UpdateExpression="SET enabled = :f",
            ExpressionAttributeValues={":f": boolean(False)},
        )
        self.destination_open_at = time.monotonic()
        self.control_state.destination_open = True

    def release_parked(self, action: str = "RELOAD") -> float:
        with self.control_state.lock:
            self.control_state.released = True
            for slot in self.control_state.parked:
                slot["action"] = action
                slot["event"].set()
            self.control_state.reloaded_at = time.monotonic()
            return self.control_state.reloaded_at

    # -- final state --------------------------------------------------------

    def drain_check(self) -> list:
        # A receipt that was in flight when the service stopped comes back
        # within one visibility window; wait that out before deciding what is
        # left over.
        time.sleep(P["visibility_seconds"] + 1.0)
        leftovers = []
        deadline = time.monotonic() + P["inspection_deadline_seconds"]
        while time.monotonic() < deadline:
            batch = self.sqs.receive_message(
                QueueUrl=self.queue_url,
                MaxNumberOfMessages=P["delivery_batch_size"],
                WaitTimeSeconds=P["sqs_poll_wait_seconds"],
            ).get("Messages", [])
            if not batch:
                break
            for message in batch:
                try:
                    seq = json.loads(message["Body"]).get("seq")
                except Exception:
                    seq = "?"
                leftovers.append((seq, message["MessageId"]))
                self.sqs.change_message_visibility(
                    QueueUrl=self.queue_url,
                    ReceiptHandle=message["ReceiptHandle"],
                    VisibilityTimeout=P["release_visibility_seconds"],
                )
            break  # anything still on the stream after done is the finding
        return leftovers

    def queue_attributes(self) -> dict:
        answer = self.sqs.get_queue_attributes(
            QueueUrl=self.queue_url, AttributeNames=["All"]
        )["Attributes"]
        return answer

    def collect_requests(self) -> list:
        items = []
        paginator = self.ddb.get_paginator("query")
        for page in paginator.paginate(
            TableName=self.table,
            KeyConditionExpression="pk = :r and begins_with(sk, :p)",
            ExpressionAttributeValues={":r": s(self.run_id), ":p": s("REQ#")},
            ConsistentRead=True,
            PaginationConfig={"PageSize": 6},
        ):
            items.extend(page.get("Items", []))
        records = []
        for item in items:
            records.append(
                {
                    "sk": item["sk"]["S"],
                    "status": int(item.get("status_code", {}).get("N", 0)),
                    "seqs": [
                        int(x)
                        for x in item.get("seqs", {}).get("S", "").split(",")
                        if x != ""
                    ],
                    "endpoint": item.get("endpoint", {}).get("S", ""),
                    "committed": item.get("committed", {}).get("BOOL", False),
                }
            )
        records.sort(key=lambda record: record["sk"])
        return records


# --------------------------------------------------------------------------- #
# verification
# --------------------------------------------------------------------------- #


def verify_deliveries(phase: Phase, records: list) -> None:
    expected = list(range(1, phase.event_count + 1))
    committed = []
    malformed = []
    for record in records:
        if record["endpoint"] != phase.endpoint_id:
            malformed.append("request %s went to %s" % (record["sk"], record["endpoint"]))
        if record["status"] == P["success_status"]:
            committed.extend(record["seqs"])
            if record["seqs"] != sorted(record["seqs"]) or len(set(record["seqs"])) != len(
                record["seqs"]
            ):
                malformed.append(
                    "request %s carried events out of order: %s"
                    % (record["sk"], record["seqs"])
                )
    if malformed:
        phase.failures.append("delivery shape: " + "; ".join(malformed))
    duplicates = sorted({seq for seq in committed if committed.count(seq) > 1})
    if duplicates:
        phase.failures.append(
            "events delivered more than once: %s" % ", ".join(str(x) for x in duplicates)
        )
    if committed != expected:
        missing = [seq for seq in expected if seq not in committed]
        unexpected = [seq for seq in committed if seq not in expected]
        divergence = next(
            (index for index, (a, b) in enumerate(zip(committed, expected)) if a != b),
            min(len(committed), len(expected)),
        )
        phase.failures.append(
            "commit order wrong: the receiver's accepted order is %s (expected %s); "
            "first divergence at position %d; missing %s; unexpected %s"
            % (
                committed,
                expected,
                divergence + 1,
                missing or "none",
                unexpected or "none",
            )
        )
    head_first = committed.index(P["head_sequence"]) if P["head_sequence"] in committed else None
    if head_first is not None and head_first > 0:
        phase.report.append(
            "events accepted before the blocked head: %s"
            % committed[:head_first]
        )


def verify_inspection(phase: Phase, inspection: dict) -> None:
    if not inspection["found"]:
        phase.failures.append(
            "inspection (%s): %s" % (inspection["tag"], inspection["note"])
        )
        return
    if not inspection["matches"]:
        phase.failures.append(
            "inspection (%s): %s (record %s, accepted record %s, saw %s)"
            % (
                inspection["tag"],
                inspection["note"],
                inspection.get("message_id"),
                inspection.get("expected_message_id"),
                inspection.get("seen"),
            )
        )


def verify_queue(phase: Phase, when: str) -> None:
    attrs = phase.queue_attributes()
    expected = {
        "DelaySeconds": str(P["queue_delay_seconds"]),
        "MessageRetentionPeriod": str(P["queue_retention_seconds"]),
        "VisibilityTimeout": str(P["visibility_seconds"]),
        "FifoQueue": "true",
    }
    for key, value in expected.items():
        if attrs.get(key) != value:
            phase.failures.append(
                "stream attributes (%s): %s is %s, expected %s"
                % (when, key, attrs.get(key), value)
            )
    if "RedrivePolicy" in attrs:
        phase.failures.append("stream attributes (%s): a redrive policy appeared" % when)


# --------------------------------------------------------------------------- #
# phase drivers
# --------------------------------------------------------------------------- #


def run_live() -> tuple:
    phase = Phase("live")
    with _phase_scope(phase):
        phase.setup(P["failure_event_count"], blocked_seq=P["head_sequence"])
        worker = phase.start_worker("main")
        if not phase.barrier(worker, "live"):
            return phase.failures, phase.report
        phase.report.append(
            "run %s: %d events accepted, destination rejecting seq %s"
            % (phase.run_id, phase.event_count, P["head_sequence"])
        )
        armed = phase.wait_for(
            lambda: phase.control_state.window_start is not None, 30.0, "arm"
        )
        if not armed:
            phase.failures.append(
                "setup-invalid: no rejected delivery was reported while the destination "
                "was failing, so the observation window never opened"
            )
            return phase.failures, phase.report
        window_close = phase.control_state.window_close
        parked = phase.wait_parked()
        phase.report.append(
            "observation window opened and closed; service %s at a park point"
            % ("caught" if parked else "not caught")
        )
        if not parked:
            phase.report.append(
                "no disposition or completion announcement arrived after the window "
                "closed within the disposition deadline"
            )
        inspection = phase.inspect_head("live")
        phase.report.append(
            "head inspection: found=%s matches=%s %s"
            % (inspection["found"], inspection["matches"], inspection["note"])
        )
        verify_inspection(phase, inspection)
        phase.open_destination()
        reloaded_at = phase.release_parked()
        done = phase.wait_for(
            lambda: phase.control_state.done_seen,
            P["recovery_deadline_seconds"],
            "recovery",
        )
        if not done:
            phase.failures.append(
                "the service did not report the run complete within %.0fs of the "
                "stream being re-opened" % P["recovery_deadline_seconds"]
            )
        elif phase.control_state.done_at < reloaded_at:
            phase.failures.append(
                "the service announced the run complete while its destination was "
                "still rejecting the head"
            )
            phase.report.append(
                "the service had announced completion during the outage, "
                "%.1fs before the stream was re-opened"
                % (reloaded_at - phase.control_state.done_at)
            )
        else:
            phase.report.append(
                "run completed %.1fs after the stream was re-opened"
                % (phase.control_state.done_at - reloaded_at)
            )
        return _finish(phase, "live")


def run_crash() -> tuple:
    phase = Phase("crash")
    with _phase_scope(phase):
        phase.setup(P["failure_event_count"], blocked_seq=P["head_sequence"])
        worker = phase.start_worker("main")
        if not phase.barrier(worker, "crash"):
            return phase.failures, phase.report
        phase.report.append(
            "run %s: %d events accepted, destination rejecting seq %s"
            % (phase.run_id, phase.event_count, P["head_sequence"])
        )
        stopped = phase.wait_for(
            lambda: phase.control_state.stop_requested.is_set(), 30.0, "stop"
        )
        if not stopped:
            phase.failures.append(
                "setup-invalid: no rejected delivery was reported, so the service was "
                "never stopped at one"
            )
            return phase.failures, phase.report
        phase.kill_worker(worker)
        phase.report.append("service stopped at its first rejected delivery")
        phase.release_parked("CONTINUE")  # its calls will never be answered
        # The destination is still rejecting and the stopped process's receive
        # is still inside its visibility window, so the stream has nothing to
        # hand anyone yet. The replacement starts anyway, the way a platform
        # would start one: against the stream exactly as the stop left it.
        replacement = phase.start_worker("replacement")
        phase.control_state.ready_at = None
        if not phase.barrier(replacement, "replacement"):
            return phase.failures, phase.report
        phase.wait_for(
            lambda: phase.control_state.done_before_open,
            P["visibility_seconds"] + P["disposition_deadline_seconds"],
            "early completion",
        )
        # Freeze the replacement where it stands — unless it has already parked
        # itself by reporting the run complete — and look at the stream under
        # the original record identity, while the destination still rejects.
        with phase.control_state.lock:
            already_reported = any(
                slot["kind"] == "done" for slot in phase.control_state.parked
            )
        if not already_reported:
            with phase.control_state.lock:
                phase.control_state.freeze_pending = True
            phase.wait_for(
                lambda: any(
                    slot["kind"] == "after-disposition"
                    for slot in phase.control_state.parked
                ),
                P["visibility_seconds"] + P["disposition_deadline_seconds"] + 5.0,
                "freeze",
            )
        inspection = phase.inspect_head("crash")
        phase.report.append(
            "head inspection: found=%s matches=%s %s"
            % (inspection["found"], inspection["matches"], inspection["note"])
        )
        verify_inspection(phase, inspection)
        if phase.control_state.done_before_open:
            phase.failures.append(
                "the replacement announced the run complete while its destination "
                "was still rejecting the head"
            )
            phase.report.append(
                "the replacement reported the run complete before the stream was "
                "re-opened, over a stream whose head it had not been able to take"
            )
        else:
            phase.report.append(
                "the replacement kept working the stream against the closed destination"
            )
        phase.open_destination()
        with phase.control_state.lock:
            phase.control_state.done_seen = False
            phase.control_state.done_at = None
        reloaded_at = phase.release_parked("RELOAD")
        done = phase.wait_for(
            lambda: phase.control_state.done_seen,
            P["recovery_deadline_seconds"],
            "replacement",
        )
        if not done:
            phase.failures.append(
                "the replacement did not report the run complete within %.0fs of the "
                "stream being re-opened" % P["recovery_deadline_seconds"]
            )
        elif phase.control_state.done_at < reloaded_at:
            phase.failures.append(
                "the replacement announced the run complete before the stream was "
                "re-opened"
            )
        else:
            phase.report.append(
                "run completed %.1fs after the stream was re-opened"
                % (phase.control_state.done_at - reloaded_at)
            )
        return _finish(phase, "crash")


def run_healthy() -> tuple:
    phase = Phase("plain")
    with _phase_scope(phase):
        phase.setup(P["healthy_event_count"])
        worker = phase.start_worker("main")
        if not phase.barrier(worker, "healthy"):
            return phase.failures, phase.report
        started = phase.go_released_at
        done = phase.wait_for(
            lambda: phase.control_state.done_seen, P["healthy_deadline_seconds"], "healthy"
        )
        elapsed = time.monotonic() - started
        if not done:
            phase.failures.append(
                "the service did not report the run complete within %.0fs"
                % P["healthy_deadline_seconds"]
            )

        def _extra() -> list:
            lines = ["healthy run: %d events in %.1fs" % (phase.event_count, elapsed)]
            if phase.control_state.held_success_report:
                lines.append(
                    "the control plane sat on the first report of accepted work for "
                    "%.0fs before answering it" % P["held_success_report_seconds"]
                )
            return lines

        return _finish(phase, "healthy", extra=_extra)


class _phase_scope:
    def __init__(self, phase):
        self.phase = phase

    def __enter__(self):
        return self.phase

    def __exit__(self, *exc):
        self.phase.teardown()
        return False


def _finish(phase: Phase, label: str, extra=None) -> tuple:
    for worker in phase.workers:
        if worker.poll() is None:
            worker.terminate()
            try:
                worker.wait(timeout=5)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait()
    leftovers = phase.drain_check()
    if leftovers:
        phase.failures.append(
            "stream still holds %d record(s) after the service reported completion: %s"
            % (len(leftovers), [(seq, mid) for seq, mid in leftovers[:6]])
        )
    records = phase.collect_requests()
    verify_deliveries(phase, records)
    verify_queue(phase, "end of " + label)
    widths = [len(record["seqs"]) for record in records]
    phase.report.append(
        "receiver saw %d requests (%d accepted, widths %s)"
        % (
            len(records),
            sum(1 for record in records if record["committed"]),
            sorted(set(widths)),
        )
    )
    phase.report.append(
        "service log tail: %s" % (phase.worker_log_tail(12) or ["<no output>"])
    )
    if extra:
        phase.report.extend(extra())
    return phase.failures, phase.report


def verify_decision() -> tuple:
    failures = []
    report = []
    if not os.path.exists(DECISION_PATH):
        return ["%s is missing" % DECISION_PATH], report
    try:
        with open(DECISION_PATH, "r", encoding="utf-8") as handle:
            record = json.load(handle)
    except Exception as error:
        return ["decision.json is not readable JSON: %r" % (error,)], report
    report.append("decision: %s" % json.dumps(record)[:400])
    if record.get("requirements_compatible") is not False:
        failures.append(
            "decision: requirements_compatible is %r; the requested behaviour and the "
            "ordered contract cannot both hold for one destination"
            % (record.get("requirements_compatible"),)
        )
    if record.get("chosen_policy") != "strict_order":
        failures.append(
            "decision: chosen_policy is %r; the ordered tier keeps one delivery in "
            "flight per destination" % (record.get("chosen_policy"),)
        )
    inflight = record.get("max_inflight_per_destination")
    if not isinstance(inflight, int) or inflight != 1:
        failures.append(
            "decision: max_inflight_per_destination is %r; the contract allows exactly one "
            "unacknowledged delivery per destination" % (inflight,)
        )
    explanation = record.get("explanation")
    if not isinstance(explanation, str) or len(explanation.strip()) < 120:
        failures.append(
            "decision: explanation is missing or too short to rest on anything observed"
        )
    return failures, report


def verify_registry() -> tuple:
    """Re-read every stream this verifier created; nothing may have been
    reconfigured underneath the runs."""
    failures = []
    report = []
    sqs = aws_client("sqs")
    urls = list(QUEUE_REGISTRY)
    if not urls:
        report.append("no streams were created by the earlier phases")
        return failures, report
    for url in urls:
        attrs = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["All"])["Attributes"]
        expected = {
            "DelaySeconds": str(P["queue_delay_seconds"]),
            "MessageRetentionPeriod": str(P["queue_retention_seconds"]),
            "VisibilityTimeout": str(P["visibility_seconds"]),
            "FifoQueue": "true",
        }
        for key, value in expected.items():
            if attrs.get(key) != value:
                failures.append(
                    "stream %s: %s is %s, expected %s"
                    % (url.rsplit("/", 1)[-1], key, attrs.get(key), value)
                )
        if "RedrivePolicy" in attrs:
            failures.append("stream %s: a redrive policy appeared" % url.rsplit("/", 1)[-1])
    report.append("checked %d stream(s) after the runs" % len(urls))
    return failures, report


def run_phase(kind: str) -> tuple:
    if kind == "live":
        return run_live()
    if kind == "crash":
        return run_crash()
    if kind == "healthy":
        return run_healthy()
    raise ValueError(kind)
