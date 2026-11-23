"""Verifier-side processes: an SQS gateway in front of the emulator and a scripted fulfilment service.

Both run as threads inside the pytest process; the relay under test is a separate process that
reaches SQS only through the gateway and fulfilment only through the fake service."""

import json
import select
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

JSON_CT = "application/x-amz-json-1.0"

ALLOWED = {
    "ReceiveMessage",
    "DeleteMessage",
    "DeleteMessageBatch",
    "ChangeMessageVisibility",
    "ChangeMessageVisibilityBatch",
    "GetQueueAttributes",
    "GetQueueUrl",
    "ListQueues",
    "ListQueueTags",
}

# A forwarded ChangeMessageVisibility at or below this value is a release; its response is held
# until the message has been handed out again (or HOLD_CAP_SECONDS pass).
RELEASE_MAX_TIMEOUT = 1
HOLD_AFTER_DELIVERY_SECONDS = 0.5
HOLD_CAP_SECONDS = 3.0
# A forwarded ChangeMessageVisibility above RELEASE_MAX_TIMEOUT (an extension) takes effect at once,
# but its response comes back this many seconds later; a forwarded DeleteMessage likewise.
EXTENSION_RESPONSE_SECONDS = (2.2, 3.0)
DELETE_RESPONSE_SECONDS = 1.5


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def order_key(body):
    try:
        value = json.loads(body)
    except (TypeError, ValueError):
        return None
    return value.get("order_id") if isinstance(value, dict) else None


class Gateway:
    def __init__(self, upstream):
        self.upstream = upstream.rstrip("/")
        self.lock = threading.Condition()
        self.events = []  # every request, in order
        self.deliveries = {}  # message_id -> [delivery]
        self.by_handle = {}  # proxy handle -> delivery
        self.latest = {}  # message_id -> proxy handle
        self.blackouts = {}  # order key -> {delivery seq: seconds after that delivery}
        self.receive_calls = []
        self.extension_latency = 0.0
        self.delete_latency = 0.0
        self.server = None
        self.port = None

    # ---- configuration / observation -------------------------------------------------------
    def black_out(self, key, seq, seconds):
        with self.lock:
            self.blackouts.setdefault(key, {})[seq] = seconds

    def deliveries_of(self, message_id):
        with self.lock:
            return list(self.deliveries.get(message_id, []))

    def first_receive_time(self):
        with self.lock:
            return self.receive_calls[0] if self.receive_calls else None

    # ---- plumbing ---------------------------------------------------------------------------
    def start(self):
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                try:
                    status, headers, payload = gateway.handle(self.headers, body)
                except Exception as exc:  # never leave the client hanging
                    status, headers, payload = error(500, "InternalError", f"gateway: {exc!r}")
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                status, headers, payload = error(400, "InvalidAction", "use POST")
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.port = free_port()
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def forward(self, action, params):
        request = urllib.request.Request(
            self.upstream + "/",
            data=json.dumps(params).encode(),
            method="POST",
            headers={
                "Content-Type": JSON_CT,
                "X-Amz-Target": f"AmazonSQS.{action}",
                "Authorization": "AWS4-HMAC-SHA256 Credential=test/20250101/us-east-1/sqs/aws4_request, "
                "SignedHeaders=host;x-amz-target, Signature=0",
                "X-Amz-Date": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as err:
            return err.code, dict(err.headers), err.read()

    def log(self, **event):
        event["t"] = time.time()
        with self.lock:
            self.events.append(event)

    # ---- request handling -------------------------------------------------------------------
    def handle(self, headers, body):
        target = headers.get("X-Amz-Target")
        if not target:
            self.log(action="?", rejected=True, reason="query protocol")
            return error(400, "InvalidAction", "this endpoint accepts the SQS JSON protocol only")
        action = target.split(".")[-1]
        try:
            params = json.loads(body or b"{}")
        except ValueError:
            return error(400, "MalformedInput", "body is not JSON")

        if action not in ALLOWED:
            self.log(action=action, rejected=True)
            return error(400, "AccessDenied", f"{action} is not permitted for this principal")

        if action == "ReceiveMessage":
            return self.receive(params)
        if action == "DeleteMessage":
            return self.settle_one("DeleteMessage", params)
        if action == "ChangeMessageVisibility":
            return self.settle_one("ChangeMessageVisibility", params)
        if action in ("DeleteMessageBatch", "ChangeMessageVisibilityBatch"):
            return self.settle_batch(action, params)
        status, rheaders, payload = self.forward(action, params)
        self.log(action=action, status=status)
        return status, pass_headers(rheaders), payload

    def receive(self, params):
        params = dict(params)
        params.pop("VisibilityTimeout", None)
        with self.lock:
            self.receive_calls.append(time.time())
        status, rheaders, payload = self.forward("ReceiveMessage", params)
        if status != 200:
            self.log(action="ReceiveMessage", status=status)
            return status, pass_headers(rheaders), payload
        result = json.loads(payload or b"{}")
        now = time.time()
        delivered = []
        with self.lock:
            for message in result.get("Messages", []) or []:
                mid = message["MessageId"]
                seq = len(self.deliveries.get(mid, [])) + 1
                handle = f"ph-{uuid.uuid4().hex}"
                record = {
                    "message_id": mid,
                    "key": order_key(message.get("Body")),
                    "seq": seq,
                    "t": now,
                    "handle": handle,
                    "upstream": message["ReceiptHandle"],
                    "receive_count": (message.get("Attributes") or {}).get("ApproximateReceiveCount"),
                }
                self.deliveries.setdefault(mid, []).append(record)
                self.by_handle[handle] = record
                self.latest[mid] = handle
                message["ReceiptHandle"] = handle
                delivered.append((mid, seq))
            self.lock.notify_all()
        self.log(action="ReceiveMessage", status=200, delivered=delivered)
        return 200, {"Content-Type": JSON_CT}, json.dumps(result).encode()

    def classify(self, action, handle, timeout=None):
        """-> ('invalid'|'superseded'|'blackout'|'forward', record)"""
        with self.lock:
            record = self.by_handle.get(handle)
            if record is None:
                return "invalid", None
            if self.latest.get(record["message_id"]) != handle:
                return "superseded", record
            if action == "ChangeMessageVisibility":
                window = self.blackouts.get(record["key"], {}).get(record["seq"])
                if window is not None and time.time() - record["t"] < window:
                    return "blackout", record
            return "forward", record

    def wait_for_redelivery(self, record, since_count):
        deadline = time.time() + HOLD_CAP_SECONDS
        mid = record["message_id"]
        with self.lock:
            while time.time() < deadline:
                if len(self.deliveries.get(mid, [])) > since_count:
                    break
                self.lock.wait(timeout=0.05)
            else:
                return
        time.sleep(HOLD_AFTER_DELIVERY_SECONDS)

    def settle_one(self, action, params):
        handle = params.get("ReceiptHandle", "")
        timeout = params.get("VisibilityTimeout")
        verdict, record = self.classify(action, handle, timeout)
        base = dict(action=action, handle=handle, timeout=timeout,
                    message_id=record and record["message_id"], seq=record and record["seq"],
                    key=record and record["key"])
        if verdict == "invalid":
            self.log(**base, forwarded=False, reason="invalid")
            return error(400, "ReceiptHandleIsInvalid", f"The input receipt handle \"{handle}\" is not a valid receipt handle.")
        if verdict in ("superseded", "blackout"):
            self.log(**base, forwarded=False, reason=verdict)
            return 200, {"Content-Type": JSON_CT}, b"{}"
        with self.lock:
            count_before = len(self.deliveries.get(record["message_id"], []))
        upstream = dict(params, ReceiptHandle=record["upstream"])
        status, rheaders, payload = self.forward(action, upstream)
        self.log(**base, forwarded=True, status=status)
        if action == "ChangeMessageVisibility" and status == 200 and is_release(timeout):
            self.wait_for_redelivery(record, count_before)
        else:
            self.respond_slowly(single_action=action)
        return status, pass_headers(rheaders), payload

    def respond_slowly(self, single_action):
        delay = self.extension_latency if single_action == "ChangeMessageVisibility" else self.delete_latency
        if delay > 0:
            time.sleep(delay)

    def settle_batch(self, action, params):
        single = action.replace("Batch", "")
        entries = params.get("Entries") or []
        successful, failed, forwarded, releases = [], [], [], []
        for entry in entries:
            handle = entry.get("ReceiptHandle", "")
            timeout = entry.get("VisibilityTimeout")
            verdict, record = self.classify(single, handle, timeout)
            base = dict(action=action, handle=handle, timeout=timeout,
                        message_id=record and record["message_id"], seq=record and record["seq"],
                        key=record and record["key"])
            if verdict == "invalid":
                self.log(**base, forwarded=False, reason="invalid")
                failed.append({"Id": entry.get("Id"), "SenderFault": True, "Code": "ReceiptHandleIsInvalid",
                               "Message": "The input receipt handle is not a valid receipt handle."})
            elif verdict in ("superseded", "blackout"):
                self.log(**base, forwarded=False, reason=verdict)
                successful.append({"Id": entry.get("Id")})
            else:
                with self.lock:
                    count_before = len(self.deliveries.get(record["message_id"], []))
                forwarded.append((dict(entry, ReceiptHandle=record["upstream"]), base))
                if single == "ChangeMessageVisibility" and is_release(timeout):
                    releases.append((record, count_before))
        if forwarded:
            status, rheaders, payload = self.forward(action, dict(params, Entries=[e for e, _ in forwarded]))
            for _, base in forwarded:
                self.log(**base, forwarded=True, status=status)
            if status != 200:
                return status, pass_headers(rheaders), payload
            result = json.loads(payload or b"{}")
            successful.extend(result.get("Successful") or [])
            failed.extend(result.get("Failed") or [])
        for record, count_before in releases:
            self.wait_for_redelivery(record, count_before)
        if forwarded and not releases:
            self.respond_slowly(single_action=single)
        out = {"Successful": successful}
        if failed:
            out["Failed"] = failed
        return 200, {"Content-Type": JSON_CT}, json.dumps(out).encode()

    # ---- views for the verifier --------------------------------------------------------------
    def settles(self, message_id):
        with self.lock:
            return [e for e in self.events if e.get("message_id") == message_id and "forwarded" in e]


def is_release(timeout):
    try:
        return int(timeout) <= RELEASE_MAX_TIMEOUT
    except (TypeError, ValueError):
        return False


def pass_headers(headers):
    out = {}
    for name, value in headers.items():
        if name.lower() in ("content-type", "x-amzn-requestid", "x-amzn-query-error"):
            out[name] = value
    out.setdefault("Content-Type", JSON_CT)
    return out


def error(status, code, message):
    payload = json.dumps({"__type": f"com.amazonaws.sqs#{code}", "message": message}).encode()
    return status, {"Content-Type": JSON_CT, "x-amzn-query-error": f"{code};Sender"}, payload


class Fulfilment:
    """Scripted fulfilment service. Roles by order key:

    hold_fault: first call held `hold` s then 500; later calls `work` s then 200.
    hold_ok:    first call held `hold` s then 200; later calls `work` s then 200.
    poison:     every call `work` s then 500.
    plain / unknown: `quick` s then 200."""

    def __init__(self, hold, work=1.0, quick=0.5):
        self.hold, self.work, self.quick = hold, work, quick
        self.roles = {}
        self.lock = threading.Lock()
        self.calls = []
        self.open = {}
        self.server = None
        self.port = None

    def calls_for(self, key):
        with self.lock:
            return [dict(c) for c in self.calls if c["key"] == key]

    def all_calls(self):
        with self.lock:
            return [dict(c) for c in self.calls]

    def start(self):
        service = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                service.serve(self, raw)

            def log_message(self, *args):
                pass

        self.port = free_port()
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/work"

    def serve(self, handler, raw):
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = {}
        body = payload.get("body") if isinstance(payload, dict) else None
        key = order_key(body) if isinstance(body, str) else None
        with self.lock:
            attempt = sum(1 for c in self.calls if c["key"] == key) + 1
            overlapped = self.open.get(key, 0) > 0
            self.open[key] = self.open.get(key, 0) + 1
            call = {
                "key": key,
                "attempt": attempt,
                "message_id": payload.get("message_id") if isinstance(payload, dict) else None,
                "receive_count": payload.get("receive_count") if isinstance(payload, dict) else None,
                "start": time.time(),
                "end": None,
                "outcome": None,
                "overlapped": overlapped,
                "abandoned": False,
            }
            self.calls.append(call)
            role = self.roles.get(key, "plain")
        if role == "hold_fault":
            duration, ok = (self.hold, False) if attempt == 1 else (self.work, True)
        elif role == "hold_ok":
            duration, ok = (self.hold, True) if attempt == 1 else (self.work, True)
        elif role == "poison":
            duration, ok = self.work, False
        else:
            duration, ok = self.quick, True

        abandoned = wait_watching(handler.connection, duration)
        status = 200 if ok else 500
        data = json.dumps({"outcome": "success" if ok else "fault"}).encode()
        try:
            handler.send_response(status)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(data)))
            handler.end_headers()
            handler.wfile.write(data)
            handler.wfile.flush()
        except OSError:
            abandoned = True
        with self.lock:
            call["end"] = time.time()
            call["outcome"] = "success" if ok else "fault"
            call["abandoned"] = abandoned
            self.open[key] -= 1


def wait_watching(sock, duration):
    """Sleep `duration` seconds; True if the client hung up meanwhile."""
    deadline = time.time() + duration
    closed = False
    while True:
        left = deadline - time.time()
        if left <= 0:
            return closed
        if closed:
            time.sleep(min(left, 0.2))
            continue
        try:
            readable, _, _ = select.select([sock], [], [], min(left, 0.2))
            if readable and sock.recv(1, socket.MSG_PEEK) == b"":
                closed = True
        except (OSError, ValueError):
            closed = True
