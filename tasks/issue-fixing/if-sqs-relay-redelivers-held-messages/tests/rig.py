"""Verifier instruments: an SQS proxy per relay, a downstream, a probe consumer, relay processes.

Every instrument stamps time.monotonic() in this one process, so all times compare directly.
Nothing here imports or reads the relay's code or logs: the relay is observed only through the
calls it makes (proxy, downstream) and through what SQS then shows the probe.
"""

import http.client
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import boto3
from pathlib import Path
from cdk_seed.cfn_deploy import deploy
from botocore.config import Config

FLOCI_HOST = os.environ.get("VERIFIER_SQS_HOST", "localhost")
FLOCI_PORT = int(os.environ.get("VERIFIER_SQS_PORT", "4566"))
FLOCI_URL = f"http://{FLOCI_HOST}:{FLOCI_PORT}"
REGION = "us-east-1"
APP_DIR = os.environ.get("RELAY_APP_DIR", "/app")

VISIBILITY_TIMEOUT = 20
WAIT_TIME_SECONDS = 2
MAX_MESSAGES_PER_POLL = 20

ALLOWED_ACTIONS = {
    "ReceiveMessage", "ChangeMessageVisibility", "ChangeMessageVisibilityBatch", "DeleteMessage",
    "DeleteMessageBatch", "GetQueueAttributes", "GetQueueUrl",
}


def now():
    return time.monotonic()


def sleep_until(deadline):
    while True:
        remaining = deadline - now()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 0.2))


def wait_for(predicate, timeout, interval=0.1):
    deadline = now() + timeout
    while now() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def floci_client():
    return boto3.client(
        "sqs", endpoint_url=FLOCI_URL, region_name=REGION, aws_access_key_id="test",
        aws_secret_access_key="test",
        config=Config(retries={"mode": "standard", "max_attempts": 3}, read_timeout=30))


# ---------------------------------------------------------------------------------------------------
# SQS protocol parsing (JSON protocol of current botocore, query protocol of older clients)

def _int_or_none(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def parse_sqs_request(headers, body):
    target = headers.get("X-Amz-Target")
    if target:
        action = target.split(".")[-1]
        try:
            params = json.loads(body or b"{}")
        except ValueError:
            params = {}
        entries = [
            {"id": e.get("Id"), "handle": e.get("ReceiptHandle"),
             "vt": _int_or_none(e.get("VisibilityTimeout"))}
            for e in params.get("Entries", []) or [] if isinstance(e, dict)
        ]
        return action, {
            "queue_url": params.get("QueueUrl"),
            "vt": _int_or_none(params.get("VisibilityTimeout")),
            "wait": _int_or_none(params.get("WaitTimeSeconds")),
            "max": _int_or_none(params.get("MaxNumberOfMessages")),
            "handle": params.get("ReceiptHandle"),
            "entries": entries,
        }
    form = {k: v[-1] for k, v in urllib.parse.parse_qs(body.decode("utf-8", "replace"),
                                                         keep_blank_values=True).items()}
    action = form.get("Action")
    grouped = {}
    for key, value in form.items():
        match = re.search(r"\.(\d+)\.(Id|ReceiptHandle|VisibilityTimeout)$", key)
        if match:
            grouped.setdefault(int(match.group(1)), {})[match.group(2)] = value
    entries = [
        {"id": e.get("Id"), "handle": e.get("ReceiptHandle"), "vt": _int_or_none(e.get("VisibilityTimeout"))}
        for _, e in sorted(grouped.items())
    ]
    return action, {
        "queue_url": form.get("QueueUrl"),
        "vt": _int_or_none(form.get("VisibilityTimeout")),
        "wait": _int_or_none(form.get("WaitTimeSeconds")),
        "max": _int_or_none(form.get("MaxNumberOfMessages")),
        "handle": form.get("ReceiptHandle"),
        "entries": entries,
    }


def parse_received_messages(data):
    text = data.decode("utf-8", "replace")
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        return [{"message_id": m.get("MessageId"), "handle": m.get("ReceiptHandle"), "body": m.get("Body")}
                for m in parsed.get("Messages", []) or []]
    found = []
    for block in re.findall(r"<Message>(.*?)</Message>", text, re.S):
        def tag(name):
            match = re.search(rf"<{name}>(.*?)</{name}>", block, re.S)
            return match.group(1) if match else None
        found.append({"message_id": tag("MessageId"), "handle": tag("ReceiptHandle"), "body": tag("Body")})
    return found


# ---------------------------------------------------------------------------------------------------
# The SQS proxy in front of Floci, one per relay

_HOP_BY_HOP = {"host", "connection", "keep-alive", "content-length", "transfer-encoding", "proxy-connection",
               "te", "trailer", "upgrade", "expect"}


class SqsProxy:
    """Forwards every call of one relay to Floci at once and logs it.

    ``gate()`` parks the relay's next ReceiveMessage requests before they reach Floci, ``release()``
    forwards them one after the other. ``hold_rule(record)`` may return a monotonic time until which
    the response (already applied by Floci) is kept from the relay.
    """

    def __init__(self, name):
        self.name = name
        self.records = []
        self.hold_rule = None
        self.first_messages_forwarded_at = None
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._gated = False
        self._gate_queue = []
        self._seq = 0
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                proxy._handle(self)

            def do_GET(self):
                proxy._handle(self)

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, name=f"proxy-{name}", daemon=True).start()

    def close(self):
        with self._cond:
            self._gated = False
            for slot in self._gate_queue:
                slot["go"] = True
            self._cond.notify_all()
        self.server.shutdown()
        self.server.server_close()

    # gate

    def gate(self):
        with self._cond:
            self._gated = True

    def gated_count(self):
        with self._lock:
            return sum(1 for slot in self._gate_queue if not slot["go"])

    def outstanding_receives(self):
        """Receives forwarded to Floci whose Floci response has not come back yet."""
        with self._lock:
            return sum(1 for r in self.records
                       if r["action"] == "ReceiveMessage" and r.get("forwarded") and r["t_floci"] is None)

    def release(self, timeout=10.0):
        """Forwards the parked receives one at a time: the next only once the previous one's response
        left, or once Floci has answered it and a rule is holding that answer back."""
        with self._cond:
            self._gated = False
            queue = list(self._gate_queue)
            self._gate_queue.clear()
        for slot in queue:
            with self._cond:
                slot["go"] = True
                self._cond.notify_all()
            deadline = now() + timeout
            while now() < deadline:
                record = slot["record"]
                if record["t_fwd"] is not None or (record["t_floci"] is not None and not record["messages"]):
                    break
                if record["held_until"] is not None:
                    break
                time.sleep(0.01)

    # request handling

    def _read_body(self, handler):
        if handler.headers.get("Transfer-Encoding", "").lower() == "chunked":
            chunks = []
            while True:
                size = int(handler.rfile.readline().strip().split(b";")[0] or b"0", 16)
                if size == 0:
                    handler.rfile.readline()
                    break
                chunks.append(handler.rfile.read(size))
                handler.rfile.readline()
            return b"".join(chunks)
        length = int(handler.headers.get("Content-Length") or 0)
        return handler.rfile.read(length) if length else b""

    def _handle(self, handler):
        body = self._read_body(handler)
        t_req = now()
        action, params = parse_sqs_request(handler.headers, body)
        with self._lock:
            self._seq += 1
            record = {"seq": self._seq, "proxy": self.name, "action": action, "t_req": t_req,
                      "vt": params["vt"], "wait": params["wait"], "max": params["max"],
                      "handle": params["handle"], "entries": params["entries"], "t_floci": None,
                      "t_fwd": None, "status": None, "messages": [], "held_until": None, "denied": False,
                      "forwarded": False}
            self.records.append(record)

        if action not in ALLOWED_ACTIONS:
            record["denied"] = True
            payload = json.dumps({"__type": "com.amazonaws.sqs#AccessDenied",
                                  "message": f"relay role is not allowed to call {action}"}).encode()
            self._respond(handler, 403, {"Content-Type": "application/x-amz-json-1.0",
                                         "x-amzn-query-error": "AccessDenied;Sender"}, payload, record)
            return

        if action == "ReceiveMessage":
            with self._cond:
                if self._gated:
                    slot = {"record": record, "go": False}
                    self._gate_queue.append(slot)
                    while not slot["go"]:
                        self._cond.wait(0.5)

        forward_headers = {k: v for k, v in handler.headers.items() if k.lower() not in _HOP_BY_HOP}
        forward_headers["Content-Length"] = str(len(body))
        record["forwarded"] = True
        try:
            connection = http.client.HTTPConnection(FLOCI_HOST, FLOCI_PORT, timeout=120)
            connection.request(handler.command, handler.path, body=body, headers=forward_headers)
            response = connection.getresponse()
            data = response.read()
            status = response.status
            response_headers = {k: v for k, v in response.getheaders() if k.lower() not in _HOP_BY_HOP}
            connection.close()
        except OSError as error:
            record["t_floci"] = now()
            record["error"] = str(error)
            self._respond(handler, 502, {"Content-Type": "text/plain"}, b"proxy upstream error", record)
            return
        record["t_floci"] = now()
        record["status"] = status
        if action == "ReceiveMessage" and status == 200:
            record["messages"] = parse_received_messages(data)
        elif action in ("ChangeMessageVisibilityBatch", "DeleteMessageBatch") and status == 200:
            try:
                parsed = json.loads(data)
                record["failed_ids"] = [f.get("Id") for f in parsed.get("Failed", []) or []]
            except (ValueError, AttributeError):
                record["failed_ids"] = []

        rule = self.hold_rule
        if rule is not None:
            try:
                until = rule(self, record)
            except Exception as error:  # a rule bug must not hang the relay
                record["rule_error"] = repr(error)
                until = None
            if until is not None and until > now():
                record["held_until"] = until
                sleep_until(until)
        self._respond(handler, status, response_headers, data, record)

    def _respond(self, handler, status, headers, data, record):
        try:
            handler.send_response(status)
            for key, value in headers.items():
                handler.send_header(key, value)
            handler.send_header("Content-Length", str(len(data)))
            handler.end_headers()
            handler.wfile.write(data)
            handler.wfile.flush()
        except OSError:
            record["client_gone"] = True
        record["t_fwd"] = now()
        if record["action"] == "ReceiveMessage" and record["messages"]:
            with self._cond:
                if self.first_messages_forwarded_at is None:
                    self.first_messages_forwarded_at = record["t_fwd"]
                self._cond.notify_all()

    def snapshot(self):
        with self._lock:
            return [dict(r) for r in self.records]


class ReceiveGapRule:
    """The empty receives of the poll that returned the first messages come back ``gap`` s after them."""

    def __init__(self, gap, window=2.0):
        self.gap = gap
        self.window = window

    def __call__(self, proxy, record):
        if record["action"] != "ReceiveMessage" or record["messages"]:
            return None
        r = proxy.first_messages_forwarded_at
        if r is None or record["t_req"] > r + self.window:
            return None
        return r + self.gap


class SlowReceiveAnswerRule:
    """The answer to the first receive that carries messages comes back ``delay`` s after Floci gave it."""

    def __init__(self, delay):
        self.delay = delay
        self.fired = False
        self._lock = threading.Lock()

    def __call__(self, proxy, record):
        if record["action"] != "ReceiveMessage" or not record["messages"]:
            return None
        with self._lock:
            if self.fired:
                return None
            self.fired = True
        return record["t_floci"] + self.delay


class SlowExtensionRule:
    """The response to the first extension carrying one of ``handles`` comes back ``delay`` s late."""

    def __init__(self, handles_of, delay):
        self.handles_of = handles_of
        self.delay = delay
        self.fired = False
        self._lock = threading.Lock()

    def __call__(self, proxy, record):
        if record["action"] not in ("ChangeMessageVisibility", "ChangeMessageVisibilityBatch"):
            return None
        handles = self.handles_of()
        carried = [record["handle"]] if record["action"] == "ChangeMessageVisibility" else [
            e["handle"] for e in record["entries"]]
        vts = [record["vt"]] if record["action"] == "ChangeMessageVisibility" else [
            e["vt"] for e in record["entries"]]
        if not any(h in handles and (vt or 0) > 0 for h, vt in zip(carried, vts)):
            return None
        with self._lock:
            if self.fired:
                return None
            self.fired = True
        return record["t_floci"] + self.delay


# ---------------------------------------------------------------------------------------------------
# The downstream

class Downstream:
    """POST /: holds each accepted call for ``plan(relay, ordinal, job)`` seconds (``ordinal`` counts the
    relay's accepted calls from 1), then answers 200.
    A second concurrent call from the same relay gets 429, as the README documents."""

    def __init__(self, plan):
        self.plan = plan
        self.records = []
        self._lock = threading.Lock()
        self._in_flight = set()
        self._accepted = {}
        self._closing = threading.Event()
        downstream = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                downstream._handle(self)

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/"
        threading.Thread(target=self.server.serve_forever, name="downstream", daemon=True).start()

    def close(self):
        self._closing.set()
        self.server.shutdown()
        self.server.server_close()

    def _handle(self, handler):
        length = int(handler.headers.get("Content-Length") or 0)
        raw = handler.rfile.read(length) if length else b""
        t_begin = now()
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = {}
        relay = payload.get("relay")
        job = None
        try:
            job = json.loads(payload.get("body") or "{}").get("job")
        except (ValueError, AttributeError, TypeError):
            pass
        with self._lock:
            busy = relay in self._in_flight
            if not busy:
                self._in_flight.add(relay)
                ordinal = self._accepted[relay] = self._accepted.get(relay, 0) + 1
            record = {"relay": relay, "job": job, "message_id": payload.get("message_id"),
                      "receipt_handle": payload.get("receipt_handle"), "t_begin": t_begin,
                      "t_end": None, "status": 429 if busy else 200, "hold": 0.0}
            self.records.append(record)
        if busy:
            record["t_end"] = now()
            self._answer(handler, 429, record)
            return
        try:
            hold = float(self.plan(relay, ordinal, job))
            record["hold"] = hold
            self._closing.wait(max(0.0, t_begin + hold - now()))
            record["t_end"] = now()
            self._answer(handler, 200, record)
        finally:
            with self._lock:
                self._in_flight.discard(relay)

    @staticmethod
    def _answer(handler, status, record):
        body = b"{}"
        try:
            handler.send_response(status)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)
            handler.wfile.flush()
        except OSError:
            record["client_gone"] = True

    def snapshot(self):
        with self._lock:
            return [dict(r) for r in self.records]


# ---------------------------------------------------------------------------------------------------
# The probe: a consumer that only looks

class Probe:
    """Receives directly from Floci with VisibilityTimeout=0 (seeing a message does not hide it)."""

    def __init__(self, queue_url):
        self.queue_url = queue_url
        self.sightings = []
        self.errors = []
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._loop, name="probe", daemon=True)
        self._thread.start()

    def _loop(self):
        client = boto3.client(
            "sqs", endpoint_url=FLOCI_URL, region_name=REGION, aws_access_key_id="test",
            aws_secret_access_key="test", config=Config(retries={"max_attempts": 1}, read_timeout=15))
        while not self._stop.is_set():
            t_req = now()
            try:
                response = client.receive_message(
                    QueueUrl=self.queue_url, MaxNumberOfMessages=10, VisibilityTimeout=0, WaitTimeSeconds=1,
                    MessageSystemAttributeNames=["ApproximateReceiveCount"])
            except Exception as error:
                self.errors.append(repr(error))
                time.sleep(0.2)
                continue
            t_resp = now()
            with self._lock:
                for message in response.get("Messages", []):
                    self.sightings.append({"t_req": t_req, "t_resp": t_resp, "message_id": message["MessageId"]})

    def stop(self):
        self._stop.set()
        self._thread.join(5)

    def snapshot(self):
        with self._lock:
            return [dict(s) for s in self.sightings]

    def seen_after(self, message_id, t):
        with self._lock:
            return any(s["message_id"] == message_id and s["t_req"] >= t for s in self.sightings)


# ---------------------------------------------------------------------------------------------------
# Relay processes

class RelayProcess:

    def __init__(self, relay_id, queue_url, proxy, downstream, log_dir, extra_env=None):
        self.relay_id = relay_id
        env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_ENDPOINT_URL")}
        env.update({
            "PYTHONPATH": APP_DIR,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "QUEUE_URL": queue_url,
            "DOWNSTREAM_URL": downstream.url,
            "RELAY_ID": relay_id,
            "AWS_ENDPOINT_URL_SQS": proxy.url,
            "AWS_ENDPOINT_URL": proxy.url,
            "AWS_REGION": REGION,
            "AWS_DEFAULT_REGION": REGION,
            "AWS_ACCESS_KEY_ID": "test",
            "AWS_SECRET_ACCESS_KEY": "test",
            "VISIBILITY_TIMEOUT": str(VISIBILITY_TIMEOUT),
            "MAX_MESSAGES_PER_POLL": str(MAX_MESSAGES_PER_POLL),
            "WAIT_TIME_SECONDS": str(WAIT_TIME_SECONDS),
            "LOG_LEVEL": "DEBUG",
        })
        env.update(extra_env or {})
        self.log_path = os.path.join(log_dir, f"{relay_id}.log")
        self._log = open(self.log_path, "wb")
        self.t_start = now()
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "relay"], cwd=tempfile.mkdtemp(prefix=f"{relay_id}-"), env=env,
            stdout=self._log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
        self.t_exit = None
        self.t_sigterm = None
        self.t_sigkill = None
        threading.Thread(target=self._watch, name=f"watch-{relay_id}", daemon=True).start()

    def _watch(self):
        self.proc.wait()
        self.t_exit = now()

    def sigterm(self):
        self.t_sigterm = now()
        try:
            os.kill(self.proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        return self.t_sigterm

    def sigkill(self):
        t = now()
        if self.t_sigkill is None:
            self.t_sigkill = t
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                self.proc.kill()
            except ProcessLookupError:
                pass
        return t

    def wait_exit(self, timeout):
        return wait_for(lambda: self.t_exit is not None, timeout)

    def finish(self, grace=20):
        if self.t_exit is None:
            if self.t_sigterm is None:
                self.sigterm()
            if not self.wait_exit(grace):
                self.sigkill()
                self.wait_exit(10)
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)  # leftovers of the process group, if any
        except (ProcessLookupError, PermissionError):
            pass
        self._log.close()


# ---------------------------------------------------------------------------------------------------
# One case: a fresh queue and its instruments

class Case:

    def __init__(self, name, plan, log_root):
        self.name = name
        self.t0 = now()
        self.log_dir = os.path.join(log_root, name)
        os.makedirs(self.log_dir, exist_ok=True)
        self.sqs = floci_client()
        self.queue_name = f"jobs-{name.lower()}-{uuid.uuid4().hex[:8]}"
        self.queue_url = deploy(
            boto3.client("cloudformation", endpoint_url=FLOCI_URL, region_name=REGION,
                         aws_access_key_id="test", aws_secret_access_key="test"),
            "seed-" + self.queue_name,
            Path(__file__).parent / "cdk_seed/seed.template.json",
            QueueName=self.queue_name, VisibilityTimeout=VISIBILITY_TIMEOUT)["QueueUrl"]
        self.downstream = Downstream(plan)
        self.probe = Probe(self.queue_url)
        self.proxies = {}
        self.relays = {}
        self.jobs = {}          # job id -> message id
        self.notes = {}

    def proxy(self, relay_id):
        proxy = SqsProxy(relay_id)
        self.proxies[relay_id] = proxy
        return proxy

    def start_relay(self, relay_id, extra_env=None):
        proxy = self.proxies.get(relay_id) or self.proxy(relay_id)
        relay = RelayProcess(relay_id, self.queue_url, proxy, self.downstream, self.log_dir, extra_env)
        self.relays[relay_id] = relay
        return relay

    def send_jobs(self, count):
        entries = []
        for index in range(count):
            job = f"{self.name.lower()}-{index}-{uuid.uuid4().hex[:10]}"
            entries.append({"Id": f"e{index}", "MessageBody": json.dumps({"job": job, "seq": index})})
        response = self.sqs.send_message_batch(QueueUrl=self.queue_url, Entries=entries)
        if response.get("Failed"):
            raise RuntimeError(f"environment: SendMessageBatch failed: {response['Failed']}")
        by_id = {e["Id"]: e for e in entries}
        sent = []
        for success in response["Successful"]:
            job = json.loads(by_id[success["Id"]]["MessageBody"])["job"]
            self.jobs[job] = success["MessageId"]
            sent.append(job)
        return sent

    def queue_counts(self):
        attributes = self.sqs.get_queue_attributes(
            QueueUrl=self.queue_url, AttributeNames=["ApproximateNumberOfMessages",
                                                     "ApproximateNumberOfMessagesNotVisible",
                                                     "ApproximateNumberOfMessagesDelayed"])["Attributes"]
        return (int(attributes.get("ApproximateNumberOfMessages", 0)),
                int(attributes.get("ApproximateNumberOfMessagesNotVisible", 0)),
                int(attributes.get("ApproximateNumberOfMessagesDelayed", 0)))

    def routed_ok(self, job):
        return any(r["job"] == job and r["status"] == 200 and r["t_end"] is not None
                   for r in self.downstream.snapshot())

    def drained(self):
        if not all(self.routed_ok(job) for job in self.jobs):
            return False
        return self.queue_counts() == (0, 0, 0)

    def drain_with(self, relay_id, quiet=60.0, cap=240.0):
        """Runs a spare relay until the queue is empty, or nothing was routed for ``quiet`` s."""
        relay = self.start_relay(relay_id)
        started = now()
        last_progress = started
        seen = 0
        while now() - started < cap:
            if self.drained():
                break
            count = sum(1 for r in self.downstream.snapshot() if r["status"] == 200 and r["t_end"])
            if count != seen:
                seen, last_progress = count, now()
            if now() - last_progress > quiet:
                break
            time.sleep(0.5)
        relay.finish()
        return relay

    def close(self):
        for relay in self.relays.values():
            relay.finish()
        self.probe.stop()
        for proxy in self.proxies.values():
            proxy.close()
        self.downstream.close()

    def evidence(self):
        def rel(value):
            return None if value is None else round(value - self.t0, 3)
        return {
            "case": self.name,
            "queue_url": self.queue_url,
            "jobs": self.jobs,
            "notes": {k: (rel(v) if isinstance(v, float) else v) for k, v in self.notes.items()},
            "relays": {rid: {"t_start": rel(r.t_start), "t_sigterm": rel(r.t_sigterm),
                             "t_sigkill": rel(r.t_sigkill), "t_exit": rel(r.t_exit),
                             "returncode": r.proc.returncode, "log": r.log_path}
                       for rid, r in self.relays.items()},
            "sqs_calls": [{**rec, "t_req": rel(rec["t_req"]), "t_floci": rel(rec["t_floci"]),
                           "t_fwd": rel(rec["t_fwd"]), "held_until": rel(rec["held_until"])}
                          for proxy in self.proxies.values() for rec in proxy.snapshot()],
            "downstream": [{**rec, "t_begin": rel(rec["t_begin"]), "t_end": rel(rec["t_end"])}
                           for rec in self.downstream.snapshot()],
            "probe": [{**s, "t_req": rel(s["t_req"]), "t_resp": rel(s["t_resp"])} for s in self.probe.snapshot()],
            "probe_errors": self.probe.errors[:20],
        }

    def dump(self, extra=None):
        data = self.evidence()
        if extra:
            data.update(extra)
        with open(os.path.join(self.log_dir, "evidence.json"), "w") as handle:
            json.dump(data, handle, indent=1, default=str)
