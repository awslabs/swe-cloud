"""A per-port HTTP fault proxy in front of Floci.

Each role process gets its own AWS_ENDPOINT_URL (http://127.0.0.1:<port>); the proxy listens on
one ephemeral port per role slot and knows which slot sent a request by the port it arrived on.
Requests are classified (DynamoDB read/write, S3 write to a canonical manifest key, S3 request on
a staging manifest key, other S3) and rules can hold a matching request until the test releases
or drops it, or answer it with a synthetic 500.
Any S3 request carrying a conditional-create or copy-source precondition header is answered 501
and recorded as a contract violation.
"""

import http.client
import http.server
import json
import re
import socket
import threading
import time
import urllib.parse
from dataclasses import dataclass, field

UPSTREAM = ("127.0.0.1", 4566)
HOLD_MAX_S = 45.0

DDB_WRITES = {"PutItem", "UpdateItem", "DeleteItem", "TransactWriteItems", "BatchWriteItem"}
CANONICAL_RE = re.compile(r"(?:^|/)_versions/(\d+)\.manifest$")
STAGING_RE = re.compile(r"(?:^|/)_versions/(\d+)\.manifest-[^/]+$")
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-connection", "transfer-encoding", "te", "trailer",
    "upgrade", "expect", "content-length",
}


@dataclass
class Request:
    seq: int
    slot: int
    method: str
    target: str
    kind: str
    op: str = ""
    bucket: str = ""
    key: str = ""
    status: int = 0
    outcome: str = "pending"  # forwarded | held-forwarded | dropped | failed500 | rejected501
    started: float = field(default_factory=time.monotonic)
    finished: float = 0.0

    def describe(self):
        what = self.op or f"{self.method} {self.key or self.bucket}"
        return f"#{self.seq} slot={self.slot} {self.kind} {what} -> {self.outcome} {self.status}"


class Rule:
    def __init__(self, slot, match, action, limit, name):
        self.slot = slot
        self.match = match
        self.action = action
        self.remaining = limit  # None: unlimited
        self.name = name
        self.held = []  # list of _Held
        self.fired = 0

    def active(self):
        return self.remaining is None or self.remaining > 0


class _Held:
    def __init__(self, request):
        self.request = request
        self.event = threading.Event()
        self.decision = "forward"


def _classify(slot, method, raw_path, headers, body, seq):
    target = headers.get("x-amz-target", "")
    if target:
        op = target.rsplit(".", 1)[-1]
        kind = "ddb_write" if op in DDB_WRITES else "ddb_read"
        return Request(seq, slot, method, target, kind, op=op)
    split = urllib.parse.urlsplit(raw_path)
    path = urllib.parse.unquote(split.path).lstrip("/")
    bucket, _, key = path.partition("/")
    if "if-none-match" in headers or any(h.startswith("x-amz-copy-source-if") for h in headers):
        kind = "violation"
    elif key and CANONICAL_RE.search(key):
        kind = "s3_canonical_write" if method in ("PUT", "POST") else "s3_canonical_read"
    elif key and STAGING_RE.search(key):
        kind = "s3_staging"
    else:
        kind = "s3_other"
    op = "CopyObject" if method == "PUT" and "x-amz-copy-source" in headers else ""
    return Request(seq, slot, method, raw_path, kind, op=op, bucket=bucket, key=key)


class FaultProxy:
    def __init__(self, slots):
        self.slots = slots
        self.ports = []
        self.lock = threading.Condition()
        self.rules = []
        self.requests = []
        self.violations = []
        self._seq = 0
        self._servers = []

    # ------------------------------------------------------------------ lifecycle

    def start(self):
        for slot in range(self.slots):
            server = _Server(("127.0.0.1", 0), _Handler)
            server.proxy = self
            server.slot = slot
            self.ports.append(server.server_address[1])
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self._servers.append(server)

    def stop(self):
        self.reset()
        for server in self._servers:
            server.shutdown()
            server.server_close()

    def reset(self):
        """Drop every still-held request, forget rules, start a fresh request log."""
        with self.lock:
            for rule in self.rules:
                rule.remaining = 0
                for held in rule.held:
                    if not held.event.is_set():
                        held.decision = "drop"
                        held.event.set()
            self.rules = []
            self.requests = []
            self.violations = []
            self.lock.notify_all()

    # ------------------------------------------------------------------ rules

    def hold(self, slot, match, limit=1, name="hold"):
        return self._add(Rule(slot, match, "hold", limit, name))

    def fail500(self, slot, match, limit=None, name="fail500"):
        return self._add(Rule(slot, match, "fail500", limit, name))

    def _add(self, rule):
        with self.lock:
            self.rules.append(rule)
        return rule

    def wait_held(self, rule, timeout, count=1):
        deadline = time.monotonic() + timeout
        with self.lock:
            while len(rule.held) < count:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self.lock.wait(min(left, 0.2))
            return True

    def release(self, rule):
        with self.lock:
            rule.remaining = 0
            for held in rule.held:
                if not held.event.is_set():
                    held.decision = "forward"
                    held.event.set()
            self.lock.notify_all()

    def drop(self, rule):
        with self.lock:
            rule.remaining = 0
            for held in rule.held:
                if not held.event.is_set():
                    held.decision = "drop"
                    held.event.set()
            self.lock.notify_all()

    def release_all(self):
        with self.lock:
            rules = list(self.rules)
        for rule in rules:
            self.release(rule)

    def disable(self, rule):
        with self.lock:
            rule.remaining = 0

    # ------------------------------------------------------------------ observations

    def completed(self, slot, kind, status_ok=True):
        with self.lock:
            return any(
                r.slot == slot and r.kind == kind and r.finished
                and (not status_ok or 200 <= r.status < 300)
                for r in self.requests
            )

    def log(self):
        with self.lock:
            return [r.describe() for r in self.requests]

    # ------------------------------------------------------------------ request path

    def _admit(self, request):
        with self.lock:
            self._seq += 1
            request.seq = self._seq
            self.requests.append(request)
            if request.kind == "violation":
                self.violations.append(request)
                return "reject", None
            for rule in self.rules:
                if rule.slot != request.slot or not rule.active():
                    continue
                if not rule.match(request, self):
                    continue
                if rule.remaining is not None:
                    rule.remaining -= 1
                rule.fired += 1
                if rule.action == "fail500":
                    return "fail500", None
                held = _Held(request)
                rule.held.append(held)
                self.lock.notify_all()
                return "hold", held
            return "forward", None


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _read_body(self):
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            chunks = []
            while True:
                size = int(self.rfile.readline().strip().split(b";")[0], 16)
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
            return b"".join(chunks)
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _handle(self):
        proxy = self.server.proxy
        body = self._read_body()
        headers = {k.lower(): v for k, v in self.headers.items()}
        request = _classify(self.server.slot, self.command, self.path, headers, body, 0)
        decision, held = proxy._admit(request)
        try:
            if decision == "reject":
                request.outcome, request.status = "rejected501", 501
                self._reply(501, b"<Error><Code>NotImplemented</Code><Message>conditional "
                                 b"headers are not supported by this gateway</Message></Error>",
                            "application/xml")
                return
            if decision == "fail500":
                request.outcome, request.status = "failed500", 500
                payload = json.dumps({
                    "__type": "com.amazonaws.dynamodb.v20120810#InternalServerError",
                    "message": "Internal server error",
                }).encode()
                self._reply(500, payload, "application/x-amz-json-1.0")
                return
            if decision == "hold":
                held.event.wait(HOLD_MAX_S)
                if held.decision == "drop":
                    request.outcome = "dropped"
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    return
                request.outcome = "held-forwarded"
            else:
                request.outcome = "forwarded"
            self._forward(request, body)
        finally:
            request.finished = time.monotonic()
            with proxy.lock:
                proxy.lock.notify_all()

    def _reply(self, status, payload, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _forward(self, request, body):
        upstream = http.client.HTTPConnection(*UPSTREAM, timeout=60)
        try:
            upstream.putrequest(self.command, self.path, skip_host=True, skip_accept_encoding=True)
            for name, value in self.headers.items():
                if name.lower() in HOP_BY_HOP:
                    continue
                upstream.putheader(name, value)
            if body or self.command in ("PUT", "POST"):
                upstream.putheader("Content-Length", str(len(body)))
            upstream.endheaders(body if body else None)
            response = upstream.getresponse()
            data = response.read()
            request.status = response.status
            self.send_response(response.status, response.reason)
            upstream_length = None
            for name, value in response.getheaders():
                lower = name.lower()
                if lower == "content-length":
                    upstream_length = value
                if lower in HOP_BY_HOP or lower in ("date", "server"):
                    continue
                self.send_header(name, value)
            if self.command == "HEAD":
                self.send_header("Content-Length", upstream_length or "0")
            else:
                self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
        except (OSError, http.client.HTTPException):
            request.outcome = "upstream-error"
            self.close_connection = True
        finally:
            upstream.close()

    do_GET = do_PUT = do_POST = do_DELETE = do_HEAD = _handle
