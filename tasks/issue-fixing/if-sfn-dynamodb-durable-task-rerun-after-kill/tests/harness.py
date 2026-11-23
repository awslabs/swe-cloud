"""Scenario driver for the durable task worker verifier.

Every try runs as its own process (fresh cwd, PYTHONPATH=/app, own process group) and reaches AWS
only through its own HTTP proxy, given to it as AWS_ENDPOINT_URL. The proxy forwards to Floci and,
like production AWS would:
  * denies calls outside the worker policy (infra/worker-policy.json);
  * applies the Standard StartExecution name rules (same name + same input while RUNNING returns
    the same execution; a closed name or a different input is ExecutionAlreadyExists; invalid
    names are InvalidName);
  * keeps an execution invisible to Describe/List/GetExecutionHistory for LAG_S after it was
    started (eventual consistency).
  * returns from ListExecutions only executions in the requested statusFilter.
It also kills the try's process group at exact request boundaries (the faults). Some task
instances start from states an earlier try left behind, seeded the way the deployed version (start
first, record after) or a try killed before it reached AWS would have left them.
"""
import http.client
import json
import os
import random
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import boto3
from pathlib import Path
from cdk_seed.cfn_deploy import deploy
from botocore.config import Config

FLOCI = os.environ.get("VERIFIER_FLOCI_ENDPOINT", "http://localhost:4566")
APP_DIR = os.environ.get("VERIFIER_APP_DIR", "/app")
LOG_DIR = os.environ.get("VERIFIER_LOG_DIR", "/logs/verifier")

LAG_S = 4.0
RETRY_DELAY_S = 0.5
STOP_AFTER_S = 5.5
EXIT_AFTER_STOP_BOUND_S = 25.0
KILL_NEXT_REQUEST_TIMEOUT_S = 10.0
FAULT_WAIT_S = 40.0
RECONNECT_SETTLE_S = 7.0
REAPER_SETTLE_S = 2.0
FINISH_BOUND_S = 45.0
CLEAR_BOUND_S = 25.0
WAIT_MODE_JOB_SECONDS = 16

EXTRA_TRY_BOUND_S = 120.0
AUTO_COMPLETE_AFTER_S = 8.0

LOST_ACK = "lost_ack"
KILL_AFTER_ACK = "kill_after_ack"
KILL_AT_FIRST = "kill_at_first_request"

_u = urllib.parse.urlparse(FLOCI)
FLOCI_HOST, FLOCI_PORT = _u.hostname, _u.port or 80


class EnvironmentProblem(Exception):
    """The emulator cannot run the scenario at all (not a verdict on the submission)."""


def _client(service):
    return boto3.client(
        service,
        endpoint_url=FLOCI,
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
        config=Config(retries={"mode": "standard", "max_attempts": 5}, read_timeout=30),
    )


def _now():
    return time.monotonic()


def _jitter(s):
    return max(0.0, s + random.uniform(-0.2, 0.2))


def _epoch(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _json_equal(a, b):
    try:
        return json.loads(a or "{}") == json.loads(b or "{}")
    except (TypeError, ValueError):
        return a == b


_BAD_NAME_CHARS = set('<>{}[]?*"#%\\^|~`$&,;:/')


def valid_execution_name(name):
    if not isinstance(name, str) or not 1 <= len(name) <= 80:
        return False
    for c in name:
        o = ord(c)
        if c.isspace() or c in _BAD_NAME_CHARS or o < 0x20 or 0x7F <= o <= 0x9F or 0xD800 <= o <= 0xDFFF:
            return False
        if o in (0xFFFE, 0xFFFF):
            return False
    return True


def ti_identity_of(input_text):
    try:
        doc = json.loads(input_text or "{}")
        ti = doc.get("task_instance") if isinstance(doc, dict) else None
        if not isinstance(ti, dict):
            return None
        return (str(ti["dag_id"]), str(ti["task_id"]), str(ti["run_id"]), int(ti["map_index"]))
    except (TypeError, ValueError, KeyError):
        return None


# --------------------------------------------------------------------------------------------
# Shared world: what every proxy knows
# --------------------------------------------------------------------------------------------
class World:
    def __init__(self, sm_arn, table, arn_formula_ok):
        self.sm_arn = sm_arn
        self.sm_name = sm_arn.rsplit(":", 1)[1]
        self.exec_prefix = sm_arn.replace(":stateMachine:", ":execution:", 1) + ":"
        self.arn_formula_ok = arn_formula_ok
        self.table = table
        self.sfn = _client("stepfunctions")
        self.lock = threading.RLock()
        self.start_lock = threading.Lock()
        self.started = {}
        # arn -> task instance label: executions whose invisibility (LAG_S) is counted from the
        # first time a try of that task instance looks at Step Functions.
        self.unseen = {}
        self.events = []
        self.t0 = _now()

    def event(self, who, kind, **detail):
        with self.lock:
            self.events.append({"t": round(_now() - self.t0, 3), "who": who, "kind": kind, **detail})

    def register_start(self, arn, name, input_text, who):
        with self.lock:
            if arn in self.started:
                return False
            self.started[arn] = {"t": _now(), "name": name, "input": input_text, "who": who}
            return True

    def lagged(self, arn):
        with self.lock:
            s = self.started.get(arn)
            return bool(s) and _now() - s["t"] < LAG_S

    def first_look(self, who):
        """A try is about to read Step Functions: start the lag of its task instance's unseen
        executions now, so what it sees does not depend on how fast the process came up."""
        with self.lock:
            for arn, label in list(self.unseen.items()):
                if who.startswith(label + "-"):
                    del self.unseen[arn]
                    self.started[arn]["t"] = _now()
                    self.events.append({"t": round(_now() - self.t0, 3), "who": who, "kind": "first_look", "arn": arn})

    def status_of(self, entry):
        """Status of a ListExecutions entry (true state)."""
        if entry.get("status"):
            return entry["status"]
        try:
            return self.sfn.describe_execution(executionArn=entry["executionArn"])["status"]
        except Exception:  # noqa: BLE001
            return None

    def lookup(self, name):
        """Describe the execution called ``name`` on Floci (true state, no lag), or None."""
        if self.arn_formula_ok:
            try:
                return self.sfn.describe_execution(executionArn=self.exec_prefix + name)
            except self.sfn.exceptions.ExecutionDoesNotExist:
                return None
        for page in self.sfn.get_paginator("list_executions").paginate(stateMachineArn=self.sm_arn):
            for ex in page.get("executions", []):
                if ex.get("name") == name:
                    return self.sfn.describe_execution(executionArn=ex["executionArn"])
        return None

    def authorize(self, target, payload):
        """None when the worker policy allows the call, else the denial message."""
        service, _, action = target.partition(".")
        ok = False
        if service == "AWSStepFunctions":
            if action in ("StartExecution", "ListExecutions", "DescribeStateMachine"):
                ok = payload.get("stateMachineArn") == self.sm_arn
            elif action in ("DescribeExecution", "GetExecutionHistory"):
                arn = str(payload.get("executionArn", ""))
                ok = arn.startswith(self.exec_prefix) or (
                    not self.arn_formula_ok and f":{self.sm_name}:" in arn
                )
        elif service == "DynamoDB_20120810":
            if action in (
                "GetItem", "PutItem", "UpdateItem", "DeleteItem", "Query", "DescribeTable",
                "ConditionCheckItem",
            ):
                ok = payload.get("TableName") == self.table
            elif action == "BatchGetItem":
                ok = set((payload.get("RequestItems") or {}).keys()) <= {self.table}
            elif action in ("TransactGetItems", "TransactWriteItems"):
                ok = all(
                    isinstance(op, dict) and op.get("TableName") == self.table
                    for item in payload.get("TransactItems") or []
                    for op in item.values()
                )
        if ok:
            return None
        return f"User is not authorized to perform: {target or 'this request'} (durable-task-worker policy)"


# --------------------------------------------------------------------------------------------
# Per-try proxy
# --------------------------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # quiet
        pass

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def do_POST(self):
        self.server.attempt_proxy.handle(self, self._body())

    def _deny(self):
        self._body()
        self.server.attempt_proxy.reply_error(self, 403, "AccessDeniedException", "not allowed")

    do_GET = do_PUT = do_DELETE = do_HEAD = do_PATCH = _deny


class AttemptProxy:
    def __init__(self, world, label, fault):
        self.world = world
        self.label = label
        self.fault = fault
        self.fired = False
        self.kill_on_next = False
        self.dead = False
        self.lock = threading.RLock()
        self.kill_lock = threading.Lock()
        self.ready = threading.Event()
        self.proc = None
        self.forwarded_starts = []
        self.replays = 0
        self.refused_starts = 0
        self.denied = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.daemon_threads = True
        self.server.attempt_proxy = self
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def attach(self, proc):
        self.proc = proc
        self.ready.set()

    def close(self):
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:  # noqa: BLE001
            pass

    def kill(self, why):
        with self.kill_lock:
            if self.dead:
                return
            self.dead = True
        self.world.event(self.label, "killed", why=why)
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            self.proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            pass

    def _kill_if_still_armed(self):
        with self.lock:
            armed = self.kill_on_next
            self.kill_on_next = False
        if armed:
            self.kill("kill_after_ack: no further request")

    # -- responses ---------------------------------------------------------------------------
    def reply(self, h, status, headers, data):
        h.send_response(status)
        for k, v in headers:
            if k.lower() not in ("content-length", "transfer-encoding", "connection", "date", "server"):
                h.send_header(k, v)
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)

    def reply_error(self, h, status, code, message):
        data = json.dumps({"__type": code, "message": message}).encode()
        self.reply(
            h,
            status,
            [
                ("Content-Type", "application/x-amz-json-1.0"),
                ("x-amzn-ErrorType", code),
                ("x-amzn-RequestId", str(uuid.uuid4())),
            ],
            data,
        )

    def reply_json(self, h, doc):
        self.reply(
            h,
            200,
            [("Content-Type", "application/x-amz-json-1.0"), ("x-amzn-RequestId", str(uuid.uuid4()))],
            json.dumps(doc).encode(),
        )

    def forward(self, h, body):
        conn = http.client.HTTPConnection(FLOCI_HOST, FLOCI_PORT, timeout=60)
        try:
            headers = {
                k: v
                for k, v in h.headers.items()
                if k.lower() not in ("connection", "content-length", "accept-encoding", "expect", "keep-alive")
            }
            conn.request("POST", h.path, body=body, headers=headers)
            r = conn.getresponse()
            return r.status, r.getheaders(), r.read()
        finally:
            conn.close()

    # -- request handling --------------------------------------------------------------------
    def handle(self, h, body):
        if not self.ready.wait(15):
            h.close_connection = True
            return
        target = h.headers.get("X-Amz-Target", "")
        with self.lock:
            if self.dead:
                h.close_connection = True
                return
            if self.kill_on_next:
                self.kill_on_next = False
                self.world.event(self.label, "fault", fault=KILL_AFTER_ACK, held=target)
                self.kill("kill_after_ack")
                h.close_connection = True
                return
            if self.fault == KILL_AT_FIRST and not self.fired:
                self.fired = True
                self.world.event(self.label, "fault", fault=KILL_AT_FIRST, held=target)
                self.kill("kill_at_first_request")
                h.close_connection = True
                return
        try:
            payload = json.loads(body or b"{}")
            if not isinstance(payload, dict):
                payload = {}
        except ValueError:
            payload = {}
        denied = self.world.authorize(target, payload)
        if denied:
            self.denied.append(target)
            self.world.event(self.label, "denied", target=target)
            return self.reply_error(h, 400, "AccessDeniedException", denied)
        action = target.split(".", 1)[1] if "." in target else ""
        if target.startswith("AWSStepFunctions."):
            if action == "StartExecution":
                return self.start_execution(h, body, payload)
            arn = payload.get("executionArn")
            if action in ("DescribeExecution", "GetExecutionHistory", "ListExecutions"):
                self.world.first_look(self.label)
            if action in ("DescribeExecution", "GetExecutionHistory") and self.world.lagged(arn):
                return self.reply_error(h, 400, "ExecutionDoesNotExist", f"Execution Does Not Exist: '{arn}'")
            status, headers, data = self.forward(h, body)
            if action == "ListExecutions" and status == 200:
                try:
                    doc = json.loads(data)
                    doc["executions"] = [
                        e for e in doc.get("executions", []) if not self.world.lagged(e.get("executionArn"))
                    ]
                    wanted = payload.get("statusFilter")
                    if wanted:
                        # AWS returns only executions in the requested status.
                        doc["executions"] = [e for e in doc["executions"] if self.world.status_of(e) == wanted]
                    data = json.dumps(doc).encode()
                except ValueError:
                    pass
            return self.reply(h, status, headers, data)
        status, headers, data = self.forward(h, body)
        return self.reply(h, status, headers, data)

    def start_execution(self, h, body, payload):
        name = payload.get("name")
        input_text = payload.get("input") or "{}"
        if name is not None and not valid_execution_name(name):
            self.world.event(self.label, "start_invalid_name", name=name)
            return self.reply_error(h, 400, "InvalidName", f"Invalid Name: '{name}'")
        new = False
        arn = None
        with self.world.start_lock:
            if name:
                existing = self.world.lookup(name)
                if existing is not None:
                    arn = existing["executionArn"]
                    if existing.get("status") == "RUNNING" and _json_equal(existing.get("input"), input_text):
                        self.replays += 1
                        self.world.event(self.label, "start_replay", arn=arn)
                        return self.reply_json(
                            h, {"executionArn": arn, "startDate": _epoch(existing.get("startDate"))}
                        )
                    self.refused_starts += 1
                    self.world.event(self.label, "start_refused", arn=arn, status=existing.get("status"))
                    return self.reply_error(
                        h, 400, "ExecutionAlreadyExists", f"Execution Already Exists: '{arn}'"
                    )
            status, headers, data = self.forward(h, body)
            if status == 200:
                try:
                    arn = json.loads(data)["executionArn"]
                except (ValueError, KeyError):
                    arn = None
                if arn:
                    new = self.world.register_start(arn, name, input_text, self.label)
        self.world.event(self.label, "start", status=status, arn=arn, new=new)
        if new:
            self.forwarded_starts.append(arn)
            if self.fault == LOST_ACK and not self.fired:
                self.fired = True
                self.world.event(self.label, "fault", fault=LOST_ACK, arn=arn)
                self.kill("lost_ack")
                h.close_connection = True
                return
            if self.fault == KILL_AFTER_ACK and not self.fired:
                self.fired = True
                with self.lock:
                    self.kill_on_next = True
                threading.Timer(KILL_NEXT_REQUEST_TIMEOUT_S, self._kill_if_still_armed).start()
        return self.reply(h, status, headers, data)


# --------------------------------------------------------------------------------------------
# Task instances and tries
# --------------------------------------------------------------------------------------------
class TI:
    def __init__(self, label, dag_id, task_id, run_id, map_index):
        self.label = label
        self.dag_id = dag_id
        self.task_id = task_id
        self.run_id = run_id
        self.map_index = map_index

    @property
    def identity(self):
        return (self.dag_id, self.task_id, self.run_id, self.map_index)

    @property
    def key(self):
        return json.dumps([self.dag_id, self.task_id, self.run_id, self.map_index], separators=(",", ":"))

    def as_dict(self):
        return {"dag_id": self.dag_id, "task_id": self.task_id, "run_id": self.run_id, "map_index": self.map_index}


class Attempt:
    def __init__(self, scenario, ti, try_number, max_tries, fault=None):
        self.ti = ti
        self.try_number = try_number
        self.label = f"{ti.label}-try{try_number}"
        self.fault = fault
        self.proxy = AttemptProxy(scenario.world, self.label, fault)
        env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_ENDPOINT_URL")}
        env.update(
            AWS_ENDPOINT_URL=f"http://127.0.0.1:{self.proxy.port}",
            AWS_DEFAULT_REGION="us-east-1",
            AWS_ACCESS_KEY_ID="test",
            AWS_SECRET_ACCESS_KEY="test",
            PYTHONPATH=APP_DIR,
            PYTHONDONTWRITEBYTECODE="1",
        )
        cmd = [
            sys.executable, "-m", "durable_task", "run",
            "--dag-id", ti.dag_id, "--task-id", ti.task_id, "--run-id", ti.run_id,
            "--map-index", str(ti.map_index), "--try-number", str(try_number),
            "--max-tries", str(max_tries),
            "--state-machine-arn", scenario.sm_arn, "--table", scenario.table,
        ]
        os.makedirs(os.path.join(LOG_DIR, "attempts"), exist_ok=True)
        self.log_path = os.path.join(LOG_DIR, "attempts", f"{self.label}.log")
        self._log = open(self.log_path, "wb")
        self.started_at = _now()
        self.ended_at = None
        self.timed_out = False
        scenario.world.event(self.label, "spawn", fault=fault)
        self.proc = subprocess.Popen(
            cmd, cwd=tempfile.mkdtemp(prefix="try-"), env=env, stdout=self._log,
            stderr=subprocess.STDOUT, start_new_session=True,
        )
        self.proxy.attach(self.proc)
        self.world = scenario.world

    def wait(self, timeout):
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.timed_out = True
            self.world.event(self.label, "timeout", after_s=timeout)
        self._end()
        return self.outcome

    def wait_fault_or_exit(self, timeout):
        return self.wait(timeout)

    def _end(self):
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            self.proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            pass
        if self.ended_at is None:
            self.ended_at = _now()
            self.world.event(self.label, "ended", outcome=self.outcome)
        self.proxy.close()
        try:
            self._log.close()
        except Exception:  # noqa: BLE001
            pass

    @property
    def outcome(self):
        if self.proxy.dead:
            return "killed"
        if self.timed_out:
            return "timeout"
        return self.proc.returncode

    def summary(self, t0):
        return {
            "ti": self.ti.label,
            "try": self.try_number,
            "fault": self.fault,
            "fired": self.proxy.fired,
            "outcome": self.outcome,
            "started_s": round(self.started_at - t0, 3),
            "ended_s": None if self.ended_at is None else round(self.ended_at - t0, 3),
            "forwarded_starts": list(self.proxy.forwarded_starts),
            "replays": self.proxy.replays,
            "refused_starts": self.proxy.refused_starts,
            "denied": list(self.proxy.denied),
            "log": self.log_path,
        }


# --------------------------------------------------------------------------------------------
# Job control: how executions of the verifier's job state machine finish
# --------------------------------------------------------------------------------------------
def _token_definition(queue_url):
    return json.dumps({
        "StartAt": "Load",
        "States": {
            "Load": {
                "Type": "Task",
                "Resource": "arn:aws:states:::sqs:sendMessage.waitForTaskToken",
                "Parameters": {
                    "QueueUrl": queue_url,
                    "MessageBody": {"token.$": "$$.Task.Token", "execution.$": "$$.Execution.Id"},
                },
                "End": True,
            }
        },
    })


def _wait_definition(seconds):
    return json.dumps({
        "StartAt": "Load",
        "States": {"Load": {"Type": "Wait", "Seconds": seconds, "Next": "Done"}, "Done": {"Type": "Succeed"}},
    })


def _parse_token_message(body):
    doc = json.loads(body)
    if isinstance(doc, str):
        doc = json.loads(doc)
    return doc.get("execution"), doc.get("token")


class TokenPump:
    """Collects task tokens (execution ARN -> token) from the job's queue."""

    def __init__(self, sqs, queue_url):
        self.sqs = sqs
        self.queue_url = queue_url
        self.tokens = {}
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stop.is_set():
            try:
                resp = self.sqs.receive_message(
                    QueueUrl=self.queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=1
                )
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
                continue
            for m in resp.get("Messages", []):
                try:
                    arn, token = _parse_token_message(m["Body"])
                    if arn and token:
                        with self.lock:
                            self.tokens[arn] = token
                except (ValueError, AttributeError, TypeError):
                    pass
                try:
                    self.sqs.delete_message(QueueUrl=self.queue_url, ReceiptHandle=m["ReceiptHandle"])
                except Exception:  # noqa: BLE001
                    pass

    def token_for(self, arn, timeout):
        deadline = _now() + timeout
        while _now() < deadline:
            with self.lock:
                if arn in self.tokens:
                    return self.tokens[arn]
            time.sleep(0.2)
        return None


def preflight():
    """Probe the emulator. Returns (mode, arn_formula_ok, states_role_arn)."""
    try:
        with urllib.request.urlopen(f"{FLOCI}/_floci/health", timeout=10) as r:
            if r.status != 200:
                raise EnvironmentProblem(f"health returned {r.status}")
    except Exception as exc:  # noqa: BLE001
        raise EnvironmentProblem(f"emulator not healthy: {exc}") from exc
    iam, sqs, sfn = _client("iam"), _client("sqs"), _client("stepfunctions")
    suffix = uuid.uuid4().hex[:8]
    trust = json.dumps({
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Principal": {"Service": "states.amazonaws.com"}, "Action": "sts:AssumeRole"}],
    })
    role_arn = deploy(_client("cloudformation"), "seed-verifier-states-" + suffix,
                      Path(__file__).parent / "cdk_seed/role.template.json",
                      RoleName=f"verifier-states-{suffix}",
                      Service="states.amazonaws.com")["RoleArn"]
    notes = []

    def wait_status(arn, wanted, timeout):
        deadline = _now() + timeout
        st = None
        while _now() < deadline:
            st = sfn.describe_execution(executionArn=arn)["status"]
            if st in wanted:
                return st
            time.sleep(0.3)
        return st

    arn_formula_ok = None
    mode = None
    can_fail = False
    # token mode
    try:
        q = sqs.create_queue(QueueName=f"verifier-probe-{suffix}")["QueueUrl"]
        sm = sfn.create_state_machine(
            name=f"probe-token-{suffix}", definition=_token_definition(q), roleArn=role_arn, type="STANDARD"
        )["stateMachineArn"]
        name = f"probe-{uuid.uuid4().hex[:12]}"
        arn = sfn.start_execution(stateMachineArn=sm, name=name, input="{}")["executionArn"]
        arn_formula_ok = arn == sm.replace(":stateMachine:", ":execution:", 1) + ":" + name
        token = None
        deadline = _now() + 12
        while _now() < deadline and token is None:
            for m in sqs.receive_message(QueueUrl=q, MaxNumberOfMessages=10, WaitTimeSeconds=1).get("Messages", []):
                ex, tok = _parse_token_message(m["Body"])
                if ex == arn and tok:
                    token = tok
        if token is None:
            notes.append("token mode: no task token arrived")
        else:
            st = sfn.describe_execution(executionArn=arn)["status"]
            if st != "RUNNING":
                notes.append(f"token mode: execution {st} while waiting for its token")
            else:
                sfn.send_task_success(taskToken=token, output="{}")
                st = wait_status(arn, ("SUCCEEDED", "FAILED", "ABORTED", "TIMED_OUT"), 10)
                if st == "SUCCEEDED":
                    arn2 = sfn.start_execution(stateMachineArn=sm, name=f"probe-{uuid.uuid4().hex[:12]}", input="{}")["executionArn"]
                    time.sleep(0.5)
                    sfn.stop_execution(executionArn=arn2)
                    if wait_status(arn2, ("ABORTED",), 6) == "ABORTED":
                        mode = "token"
                        can_fail = _probe_task_failure(sfn, sqs, sm, q, wait_status, notes)
                    else:
                        notes.append("token mode: StopExecution did not abort")
                else:
                    notes.append(f"token mode: after SendTaskSuccess status {st}")
    except Exception as exc:  # noqa: BLE001
        notes.append(f"token mode probe error: {exc!r}")
    if mode is None:
        try:
            sm = sfn.create_state_machine(
                name=f"probe-wait-{suffix}", definition=_wait_definition(3), roleArn=role_arn, type="STANDARD"
            )["stateMachineArn"]
            name = f"probe-{uuid.uuid4().hex[:12]}"
            arn = sfn.start_execution(stateMachineArn=sm, name=name, input="{}")["executionArn"]
            if arn_formula_ok is None:
                arn_formula_ok = arn == sm.replace(":stateMachine:", ":execution:", 1) + ":" + name
            time.sleep(1.0)
            st1 = sfn.describe_execution(executionArn=arn)["status"]
            st2 = wait_status(arn, ("SUCCEEDED",), 12)
            arn2 = sfn.start_execution(stateMachineArn=sm, name=f"probe-{uuid.uuid4().hex[:12]}", input="{}")["executionArn"]
            time.sleep(0.5)
            sfn.stop_execution(executionArn=arn2)
            st3 = wait_status(arn2, ("ABORTED",), 6)
            if st1 == "RUNNING" and st2 == "SUCCEEDED" and st3 == "ABORTED":
                mode = "wait"
            else:
                notes.append(f"wait mode: statuses {st1}/{st2}/{st3}")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"wait mode probe error: {exc!r}")
    if mode is None:
        raise EnvironmentProblem("; ".join(notes))
    if not can_fail:
        notes.append("cannot fail an execution: the deployed-version FAILED cases are not run")
    return mode, bool(arn_formula_ok), role_arn, notes, can_fail


def _probe_task_failure(sfn, sqs, sm, q, wait_status, notes):
    """Can the verifier make an execution FAILED (SendTaskFailure on its token)?"""
    try:
        arn = sfn.start_execution(stateMachineArn=sm, name=f"probe-{uuid.uuid4().hex[:12]}", input="{}")["executionArn"]
        token = None
        deadline = _now() + 12
        while _now() < deadline and token is None:
            for m in sqs.receive_message(QueueUrl=q, MaxNumberOfMessages=10, WaitTimeSeconds=1).get("Messages", []):
                ex, tok = _parse_token_message(m["Body"])
                if ex == arn and tok:
                    token = tok
        if token is None:
            notes.append("task failure probe: no token")
            return False
        sfn.send_task_failure(taskToken=token, error="LoadFailed", cause="probe")
        st = wait_status(arn, ("FAILED", "SUCCEEDED", "ABORTED", "TIMED_OUT"), 10)
        if st == "FAILED":
            return True
        notes.append(f"task failure probe: status {st}")
    except Exception as exc:  # noqa: BLE001
        notes.append(f"task failure probe error: {exc!r}")
    return False


# --------------------------------------------------------------------------------------------
# The scenario
# --------------------------------------------------------------------------------------------
def _make_tis():
    dag_id = random.choice(["etl", "orders_etl", "ledger_sync", "wh_daily"])
    task_id = random.choice(["load", "load_orders", "merge_partition"])
    day = datetime(2026, random.randint(1, 9), random.randint(1, 28), tzinfo=timezone.utc)
    hh, mm = random.randint(0, 23), random.choice([0, 15, 30, 45])
    manual = f"manual__{day:%Y-%m-%d}T{hh:02d}:{mm:02d}:00+00:00"
    lookalike = manual.replace(":", "_").replace("+", "_")
    scheduled = f"scheduled__{day:%Y-%m-%d}T00:00:00+00:00"
    prev = datetime.fromtimestamp(day.timestamp() - 86400, tz=timezone.utc)
    older = f"scheduled__{prev:%Y-%m-%d}T00:00:00+00:00"
    def days_before(n):
        d = datetime.fromtimestamp(day.timestamp() - n * 86400, tz=timezone.utc)
        return f"scheduled__{d:%Y-%m-%d}T00:00:00+00:00"

    return {
        "TI1": TI("TI1", dag_id, task_id, manual, -1),
        "TI2": TI("TI2", dag_id, task_id, lookalike, -1),
        "TI3": TI("TI3", dag_id, task_id, scheduled, random.randint(1, 7)),
        "TIL": TI("TIL", dag_id, task_id, older, -1),
        # first try (this version) killed before it reached AWS: nothing anywhere
        "TIK": TI("TIK", dag_id, task_id, days_before(2), -1),
        # first try (deployed version) killed after its start, before it wrote the record
        "TIW": TI("TIW", dag_id, task_id, days_before(3), -1),
        # deployed version: try 1's run FAILED and was recorded; try 2 died before reaching AWS
        "TIG": TI("TIG", dag_id, task_id, days_before(4), -1),
        # deployed version: try 1's run FAILED and was recorded; try 2 started another and died
        # before it wrote the record
        "TIW2": TI("TIW2", dag_id, task_id, days_before(5), -1),
        # first try (deployed version) killed after its start, before it wrote the record; the
        # job then finished successfully before AWS's read path showed the execution at all
        "TIF": TI("TIF", dag_id, task_id, days_before(6), -1),
    }


# Task instances driven next to the main timeline: label -> (first try, max tries, first fault)
EXTRA = {
    "TIK": (1, 3, KILL_AT_FIRST),
    "TIW": (2, 3, None),
    "TIG": (3, 4, None),
    "TIW2": (3, 4, None),
    "TIF": (2, 3, None),
}
FAILED_SEED = ("TIG", "TIW2")
# Needs an execution the verifier can finish on demand (token mode).
FAST_SEED = ("TIF",)


class Scenario:
    def __init__(self):
        self.mode, arn_formula_ok, self.role_arn, self.notes, self.can_fail = preflight()
        self.sfn = _client("stepfunctions")
        self.ddb = _client("dynamodb")
        self.sqs = _client("sqs")
        suffix = uuid.uuid4().hex[:8]
        self.table = f"tss-{suffix}"
        deploy(_client("cloudformation"), "seed-" + self.table,
               Path(__file__).parent / "cdk_seed/seed.template.json",
               TableName=self.table)
        for _ in range(50):
            if self.ddb.describe_table(TableName=self.table)["Table"].get("TableStatus") == "ACTIVE":
                break
            time.sleep(0.2)
        self.pump = None
        if self.mode == "token":
            q = deploy(_client("cloudformation"), "seed-verifier-job-" + suffix,
                       Path(__file__).parent / "cdk_seed/queue.template.json",
                       QueueName=f"verifier-job-{suffix}")["QueueUrl"]
            definition = _token_definition(q)
            self.pump = TokenPump(self.sqs, q)
        else:
            definition = _wait_definition(WAIT_MODE_JOB_SECONDS)
        self.sm_arn = deploy(_client("cloudformation"), "seed-job-" + suffix,
                             Path(__file__).parent / "cdk_seed/machine.template.json",
                             MachineName=f"job-{suffix}", Definition=definition,
                             RoleArn=self.role_arn)["MachineArn"]
        self.world = World(self.sm_arn, self.table, arn_formula_ok)
        self.tis = _make_tis()
        self.by_identity = {ti.identity: label for label, ti in self.tis.items()}
        self.attempts = {}
        self.desc_cache = {}
        self.running_max = {}
        self.sampling = threading.Event()
        self.seeded = []
        self.seeded_by_ti = {}
        self.extra_run = {}
        self.first_exec = {}
        self.snapshots = {}
        self.completed = set()
        self.first_seen_running = {}

    # -- observation -------------------------------------------------------------------------
    def describe(self, arn):
        d = self.desc_cache.get(arn)
        if d is not None and d.get("status") != "RUNNING":
            return d
        d = self.sfn.describe_execution(executionArn=arn)
        self.desc_cache[arn] = d
        return d

    def label_of(self, desc):
        ident = ti_identity_of(desc.get("input"))
        if ident is None:
            return "unknown"
        return self.by_identity.get(ident, "other")

    def list_arns(self, status=None):
        kw = {"stateMachineArn": self.sm_arn}
        if status:
            kw["statusFilter"] = status
        arns = []
        for page in self.sfn.get_paginator("list_executions").paginate(**kw):
            arns.extend(e["executionArn"] for e in page.get("executions", []))
        return arns

    def running_by_label(self):
        out = {}
        for arn in self.list_arns("RUNNING"):
            try:
                d = self.describe(arn)
            except Exception:  # noqa: BLE001
                continue
            if d.get("status") != "RUNNING":
                continue
            out.setdefault(self.label_of(d), []).append(arn)
        return out

    def _sampler(self):
        while self.sampling.is_set():
            try:
                for label, arns in self.running_by_label().items():
                    self.running_max[label] = max(self.running_max.get(label, 0), len(arns))
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.4)

    def snapshot(self, name):
        rb = self.running_by_label()
        self.snapshots[name] = {k: list(v) for k, v in rb.items()}
        for label, arns in rb.items():
            self.running_max[label] = max(self.running_max.get(label, 0), len(arns))
        self.world.event("verifier", "snapshot", name=name, running={k: len(v) for k, v in rb.items()})

    # -- actions -----------------------------------------------------------------------------
    def spawn(self, label, try_number, max_tries, fault=None):
        a = Attempt(self, self.tis[label], try_number, max_tries, fault)
        self.attempts[a.label] = a
        return a

    def stop(self, arn, why):
        if not arn:
            self.world.event("verifier", "stop_skipped", why=why)
            return
        try:
            self.sfn.stop_execution(executionArn=arn, error="ConsoleStop", cause=why)
            self.world.event("verifier", "stop", arn=arn, why=why)
        except Exception as exc:  # noqa: BLE001
            self.world.event("verifier", "stop_error", arn=arn, why=why, error=repr(exc))

    def complete(self, label):
        """Let every RUNNING execution of this task instance finish successfully."""
        if self.mode != "token":
            return
        for arn in self.running_by_label().get(label, []):
            token = self.pump.token_for(arn, 10)
            if token is None:
                self.world.event("verifier", "complete_no_token", arn=arn)
                continue
            try:
                self.sfn.send_task_success(taskToken=token, output="{}")
                self.world.event("verifier", "complete", arn=arn, ti=label)
            except Exception as exc:  # noqa: BLE001
                self.world.event("verifier", "complete_error", arn=arn, error=repr(exc))

    def reconcile(self, label):
        """The platform reconciler, as docs/operations.md describes it."""
        ti = self.tis[label]
        item = self.ddb.get_item(TableName=self.table, Key={"ti_key": {"S": ti.key}}, ConsistentRead=True).get("Item") or {}
        arn = (item.get("execution_arn") or {}).get("S")
        self.world.event("reconciler", "read", ti=label, execution_arn=arn)
        if arn:
            try:
                self.sfn.stop_execution(executionArn=arn, error="Reconciler", cause="task instance failed")
                self.world.event("reconciler", "stop", arn=arn)
            except Exception as exc:  # noqa: BLE001
                self.world.event("reconciler", "stop_error", arn=arn, error=repr(exc))
        return arn

    def _seed_start(self, label, lagged):
        """StartExecution as the deployed version does it: uuid name, the task instance's input."""
        ti = self.tis[label]
        name = uuid.uuid4().hex
        input_text = json.dumps({"task_instance": ti.as_dict()}, sort_keys=True, separators=(",", ":"))
        arn = self.sfn.start_execution(stateMachineArn=self.sm_arn, name=name, input=input_text)["executionArn"]
        if lagged:
            # started moments ago: AWS's read path has not caught up yet
            self.world.register_start(arn, name, input_text, "seed")
        self.seeded.append(arn)
        self.seeded_by_ti.setdefault(label, []).append(arn)
        self.world.event("verifier", "seed_start", ti=label, arn=arn, lagged=lagged)
        return arn

    def _seed_record(self, label, arn, try_number):
        ti = self.tis[label]
        self.ddb.put_item(
            TableName=self.table,
            Item={
                "ti_key": {"S": ti.key},
                "execution_arn": {"S": arn},
                "try_number": {"N": str(try_number)},
                "updated_at": {"S": datetime.now(timezone.utc).isoformat()},
            },
        )
        self.world.event("verifier", "seed_record", ti=label, arn=arn, try_number=try_number)

    def _fail(self, arn):
        token = self.pump.token_for(arn, 15)
        if token is None:
            raise EnvironmentProblem(f"no task token for seeded execution {arn}")
        self.sfn.send_task_failure(taskToken=token, error="LoadFailed", cause="warehouse load failed")
        deadline = _now() + 10
        while _now() < deadline:
            if self.sfn.describe_execution(executionArn=arn)["status"] == "FAILED":
                self.completed.add(arn)
                return
            time.sleep(0.2)
        raise EnvironmentProblem(f"seeded execution {arn} did not fail")

    def seed_failed_history(self, label):
        """Deployed version, try 1: its run FAILED, the record names it (written by try 1)."""
        arn = self._seed_start(label, lagged=False)
        self._fail(arn)
        self._seed_record(label, arn, 1)
        return arn

    def seed_succeeded_unseen(self, label):
        """Deployed version, try 1: started the job and died before recording it. The job has
        already succeeded, and Describe/List do not show the execution yet (they stay behind for
        LAG_S from the next try's first look at Step Functions)."""
        arn = self._seed_start(label, lagged=True)
        token = self.pump.token_for(arn, 15)
        if token is None:
            raise EnvironmentProblem(f"no task token for seeded execution {arn}")
        self.sfn.send_task_success(taskToken=token, output="{}")
        self.completed.add(arn)
        deadline = _now() + 10
        while _now() < deadline:
            if self.sfn.describe_execution(executionArn=arn)["status"] == "SUCCEEDED":
                with self.world.lock:
                    self.world.started[arn]["t"] = _now()
                    self.world.unseen[arn] = label
                self.world.event("verifier", "complete", arn=arn, ti=label)
                return arn
            time.sleep(0.2)
        raise EnvironmentProblem(f"seeded execution {arn} did not succeed")

    def seed_legacy(self, label):
        """An in-flight try of the deployed version: uuid name, record written after the start."""
        ti = self.tis[label]
        name = uuid.uuid4().hex
        input_text = json.dumps({"task_instance": ti.as_dict()}, sort_keys=True, separators=(",", ":"))
        arn = self.sfn.start_execution(stateMachineArn=self.sm_arn, name=name, input=input_text)["executionArn"]
        self.ddb.put_item(
            TableName=self.table,
            Item={
                "ti_key": {"S": ti.key},
                "execution_arn": {"S": arn},
                "try_number": {"N": "1"},
                "updated_at": {"S": datetime.now(timezone.utc).isoformat()},
            },
        )
        self.seeded.append(arn)
        self.seeded_by_ti.setdefault(label, []).append(arn)
        self.world.event("verifier", "seed_legacy", arn=arn)
        return arn

    def _auto_complete(self):
        """Executions of the extra task instances succeed once they have run for a while."""
        while self.sampling.is_set():
            if self.mode == "token":
                try:
                    for label, arns in self.running_by_label().items():
                        if label not in EXTRA:
                            continue
                        for arn in arns:
                            seen = self.first_seen_running.setdefault(arn, _now())
                            if arn in self.completed or _now() - seen < AUTO_COMPLETE_AFTER_S:
                                continue
                            token = self.pump.token_for(arn, 2)
                            if token is None:
                                continue
                            try:
                                self.sfn.send_task_success(taskToken=token, output="{}")
                                self.completed.add(arn)
                                self.world.event("verifier", "complete", arn=arn, ti=label)
                            except Exception as exc:  # noqa: BLE001
                                self.world.event("verifier", "complete_error", arn=arn, error=repr(exc))
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(0.5)

    def _drive_extra(self, label):
        """Tries of one task instance, one after another, until one succeeds or none are left."""
        first, max_tries, fault = EXTRA[label]
        for n in range(first, max_tries + 1):
            a = self.spawn(label, n, max_tries, fault if n == first else None)
            a.wait(EXTRA_TRY_BOUND_S)
            if a.outcome == 0:
                return
            time.sleep(_jitter(RETRY_DELAY_S))

    def _start_extra(self):
        threads = []
        for label in EXTRA:
            if label in FAILED_SEED and not self.can_fail:
                self.extra_run[label] = False
                continue
            if label in FAST_SEED and self.mode != "token":
                self.extra_run[label] = False
                continue
            self.extra_run[label] = True
            if label in FAILED_SEED:
                self.seed_failed_history(label)
            if label in FAST_SEED:
                self.seed_succeeded_unseen(label)
                time.sleep(_jitter(RETRY_DELAY_S))
            if label in ("TIW", "TIW2"):
                self._seed_start(label, lagged=True)
                time.sleep(_jitter(RETRY_DELAY_S))
            t = threading.Thread(target=self._drive_extra, args=(label,), daemon=True)
            t.start()
            threads.append(t)
        return threads

    # -- timeline ----------------------------------------------------------------------------
    def run(self):
        self.sampling.set()
        sampler = threading.Thread(target=self._sampler, daemon=True)
        sampler.start()
        completer = threading.Thread(target=self._auto_complete, daemon=True)
        completer.start()
        try:
            extra = self._start_extra()
            self._timeline()
            deadline = _now() + 2 * EXTRA_TRY_BOUND_S + 30
            for t in extra:
                t.join(timeout=max(1.0, deadline - _now()))
        finally:
            self.sampling.clear()
            sampler.join(timeout=5)
            completer.join(timeout=5)
        return self.collect()

    def _first_start(self, attempt_label):
        a = self.attempts.get(attempt_label)
        return a.proxy.forwarded_starts[0] if a and a.proxy.forwarded_starts else None

    def _timeline(self):
        self.seed_legacy("TIL")
        a1 = self.spawn("TI1", 1, 5, LOST_ACK)
        a1.wait_fault_or_exit(FAULT_WAIT_S)
        self.first_exec["TI1"] = self._first_start("TI1-try1")
        time.sleep(_jitter(RETRY_DELAY_S))

        a2 = self.spawn("TI1", 2, 5)
        b1 = self.spawn("TI2", 1, 3)
        c1 = self.spawn("TI3", 1, 2)
        l2 = self.spawn("TIL", 2, 3)
        time.sleep(_jitter(STOP_AFTER_S))
        self.first_exec["TI3"] = self._first_start("TI3-try1")
        self.snapshot("before_console_stop")
        self.stop(self.first_exec["TI1"], "console stop of TI1's first execution")
        self.stop(self.first_exec["TI3"], "console stop of TI3's first execution")
        t_stop = _now()
        a2.wait(EXIT_AFTER_STOP_BOUND_S)
        c1.wait(max(1.0, EXIT_AFTER_STOP_BOUND_S - (_now() - t_stop)))
        time.sleep(_jitter(RETRY_DELAY_S))

        a3 = self.spawn("TI1", 3, 5, KILL_AFTER_ACK)
        c2 = self.spawn("TI3", 2, 2, KILL_AFTER_ACK)
        a3.wait_fault_or_exit(FAULT_WAIT_S)
        c2.wait_fault_or_exit(FAULT_WAIT_S)
        self.reconcile("TI3")
        time.sleep(REAPER_SETTLE_S)
        self.snapshot("after_reconciler")

        a4 = self.spawn("TI1", 4, 5)
        time.sleep(_jitter(RECONNECT_SETTLE_S))
        self.snapshot("before_completion")
        for label in ("TI1", "TI2", "TIL"):
            self.complete(label)
        a4.wait(FINISH_BOUND_S)
        b1.wait(FINISH_BOUND_S)
        l2.wait(FINISH_BOUND_S)
        time.sleep(_jitter(RETRY_DELAY_S))

        self.snapshot("before_clear")
        self.ti1_before_clear = sorted(self._arns_of("TI1"))
        a5 = self.spawn("TI1", 5, 5)
        a5.wait(CLEAR_BOUND_S)
        self.snapshot("after_clear")

    def _arns_of(self, label):
        out = []
        for arn in self.list_arns():
            try:
                if self.label_of(self.describe(arn)) == label:
                    out.append(arn)
            except Exception:  # noqa: BLE001
                pass
        return out

    def collect(self):
        t0 = self.world.t0
        executions = []
        for arn in self.list_arns():
            d = self.sfn.describe_execution(executionArn=arn)
            executions.append({
                "arn": arn,
                "name": d.get("name"),
                "status": d.get("status"),
                "start": _epoch(d.get("startDate")),
                "stop": _epoch(d.get("stopDate")),
                "ti": self.label_of(d),
                "redrive_count": d.get("redriveCount"),
                "proxied": arn in self.world.started,
            })
        records = {}
        for label, ti in self.tis.items():
            item = self.ddb.get_item(TableName=self.table, Key={"ti_key": {"S": ti.key}}, ConsistentRead=True).get("Item") or {}
            records[label] = {k: list(v.values())[0] for k, v in item.items()}
        result = {
            "mode": self.mode,
            "notes": self.notes,
            "state_machine": self.sm_arn,
            "table": self.table,
            "tis": {k: v.as_dict() for k, v in self.tis.items()},
            "attempts": {k: a.summary(t0) for k, a in self.attempts.items()},
            "executions": executions,
            "running_max": self.running_max,
            "snapshots": self.snapshots,
            "seeded": self.seeded,
            "seeded_by_ti": self.seeded_by_ti,
            "extra_run": self.extra_run,
            "can_fail": self.can_fail,
            "first_exec": self.first_exec,
            "ti1_before_clear": getattr(self, "ti1_before_clear", None),
            "records": records,
            "events": self.world.events,
        }
        # leave nothing running behind
        for e in executions:
            if e["status"] == "RUNNING":
                try:
                    self.sfn.stop_execution(executionArn=e["arn"])
                except Exception:  # noqa: BLE001
                    pass
        if self.pump:
            self.pump.stop.set()
        return result


def run_scenario():
    os.makedirs(LOG_DIR, exist_ok=True)
    try:
        result = Scenario().run()
    except EnvironmentProblem as exc:
        with open(os.path.join(LOG_DIR, "environment.txt"), "w") as f:
            f.write(f"environment: {exc}\n")
        raise
    with open(os.path.join(LOG_DIR, "scenario.json"), "w") as f:
        json.dump(result, f, indent=2, default=str)
    return result
