"""A recording HTTP storage gateway that sits in front of the emulator for the test.

Each writer role talks to its own gateway port; the gateway forwards every request to the
emulator on a fresh upstream connection and records what happened to the lock object. For the
faulted role the gateway can, on chosen writes, model the flaky storage gateway the README
describes: it can reply "503 Service Unavailable" instead of forwarding, forward and then still
reply 503, forward and then close the connection without a reply, or reply "504 Gateway Timeout"
and forward the request later anyway (a slow backend request the gateway did not cancel).

The gateway never touches the emulator's data itself; it only forwards and observes, so the
version history of the lock object is exactly the sequence of writes it (and the sidecar) made.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
from urllib.parse import urlsplit, unquote

HOP_BY_HOP = {"connection", "keep-alive", "proxy-connection", "transfer-encoding", "te",
              "trailer", "upgrade", "content-length", "host", "expect"}
REQUIRED_KEYS = ("expired", "validUntil", "owner")


def norm_etag(value):
    if value is None:
        return None
    v = value.strip()
    if v.startswith("W/"):
        v = v[2:]
    return v.strip('"')


class LockWrite:
    """One accepted (HTTP 200) write to the lock object, in chain order."""

    def __init__(self, author, if_match, body, etag, t):
        self.author = author          # "w1", "w2" or "H"
        self.if_match = if_match      # the precondition ETag (None only for the seed)
        self.body = body              # parsed JSON dict
        self.etag = etag              # the ETag the emulator returned
        self.t = t                    # seconds since the rig started
        self.epoch_ms = int(time.time() * 1000)   # when the write was recorded (after landing)
        self.recv_ms = None                        # when the gateway received the request

    @property
    def owner(self):
        return self.body.get("owner")

    @property
    def expired(self):
        return bool(self.body.get("expired"))

    @property
    def valid_until(self):
        return int(self.body.get("validUntil", 0))


class Rig:
    """Shared state across the per-role gateway servers and the sidecar."""

    def __init__(self, *, bucket, lock_key, ledger_prefix, results_prefix, lease_seconds,
                 drift_buffer_ms, upstream=("127.0.0.1", 4566)):
        self.bucket = bucket
        self.lock_key = lock_key
        self.ledger_prefix = ledger_prefix
        self.results_prefix = results_prefix
        self.lease_ms = lease_seconds * 1000
        self.drift_buffer_ms = drift_buffer_ms
        self.upstream = upstream

        self.lock = threading.RLock()
        self.t0 = time.time()
        self.chain = []              # LockWrite, in the order writes were accepted (excl. seed)
        self.seed_etag = None
        self.reads = []              # (role, etag, t) for every lock GET answered 200
        self.contract_violations = []
        self.requests = []           # (role, method, key, t) coarse request log
        self.policy = None           # a FaultPolicy, or None for a clean gateway
        self.sidecar = None          # a Sidecar, attached by the test
        self.phases = {}             # phase name -> {"t": ..., "etag": ...}
        self.w2_hold = threading.Event()   # set => the rival's lock writes wait
        self.gate_timeout_s = 8.0
        self.counter_writes = []     # (author, seq, t) for every counter write that landed
        self.late_writes = []        # threads forwarding a held write after its 504

    def hold_w2(self):
        self.w2_hold.set()

    def release_w2(self):
        self.w2_hold.clear()

    def now(self):
        return time.time() - self.t0

    def lock_path(self):
        return "/" + self.bucket + "/" + self.lock_key

    def is_lock_key(self, path):
        key = unquote(urlsplit(path).path).lstrip("/")
        return key == (self.bucket + "/" + self.lock_key)

    def key_of(self, path):
        return unquote(urlsplit(path).path).lstrip("/")[len(self.bucket) + 1:]

    def counter_key(self):
        return f"{self.ledger_prefix}counter.json"

    def is_counter_key(self, path):
        key = unquote(urlsplit(path).path).lstrip("/")
        return key == (self.bucket + "/" + self.counter_key())

    def record_counter_write(self, author, body_bytes):
        try:
            seq = int(json.loads(body_bytes).get("seq"))
        except Exception:
            seq = None
        with self.lock:
            self.counter_writes.append((author, seq, self.now()))

    def upstream_counter_seq(self):
        """The counter's current seq, read straight from the emulator (no gateway)."""
        try:
            if getattr(self, "_admin", None) is None:
                import boto3
                host, port = self.upstream
                self._admin = boto3.client("s3", endpoint_url=f"http://{host}:{port}")
            resp = self._admin.get_object(Bucket=self.bucket, Key=self.counter_key())
            return int(json.loads(resp["Body"].read()).get("seq"))
        except Exception:
            self._admin = None
            return None

    def record_seed(self, etag):
        self.seed_etag = norm_etag(etag)

    def record_lock_write(self, author, if_match, body, etag, recv_ms=None):
        w = LockWrite(author, norm_etag(if_match), body, norm_etag(etag), self.now())
        w.recv_ms = recv_ms
        with self.lock:
            self.chain.append(w)
        return w

    def record_read(self, role, etag):
        with self.lock:
            self.reads.append((role, norm_etag(etag), self.now()))

    def note_violation(self, msg):
        with self.lock:
            self.contract_violations.append((self.now(), msg))


class _Upstream:
    """One forwarded round-trip to the emulator."""

    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body


def forward(rig, method, path, headers, body):
    host, port = rig.upstream
    conn = http.client.HTTPConnection(host, port, timeout=30)
    try:
        fwd = {k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP}
        fwd["Host"] = f"{host}:{port}"
        conn.request(method, path, body=body, headers=fwd)
        resp = conn.getresponse()
        data = resp.read()
        hdrs = [(k, v) for k, v in resp.getheaders() if k.lower() not in HOP_BY_HOP]
        return _Upstream(resp.status, hdrs, data)
    finally:
        conn.close()


def make_handler(rig, role):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _read_body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        def _relay(self, up):
            self.send_response(up.status)
            for k, v in up.headers:
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(up.body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(up.body)

        def _error(self, status, code, message):
            payload = (f'<?xml version="1.0" encoding="UTF-8"?><Error><Code>{code}</Code>'
                       f'<Message>{message}</Message></Error>').encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/xml")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _handle(self):
            self.recv_ms = int(time.time() * 1000)
            body = self._read_body()
            path = self.path
            headers = {k: v for k, v in self.headers.items()}
            is_lock = rig.is_lock_key(path)
            rig.requests.append((role, self.command, rig.key_of(path), rig.now()))

            # The rival's *acquire* writes wait while a phase is holding the gate open; its reads
            # and releases always pass, so a rival that already holds the lock can still release it
            # and the sidecar never deadlocks behind it.
            if role == "w2" and is_lock and self.command == "PUT" and rig.w2_hold.is_set():
                try:
                    gated = not bool(json.loads(body).get("expired"))
                except Exception:
                    gated = True
                if gated:
                    deadline = time.monotonic() + rig.gate_timeout_s
                    while rig.w2_hold.is_set() and time.monotonic() < deadline:
                        time.sleep(0.02)
            # The faulted worker reading the lock back ends the P5 gate on the rival.
            if role == "w1" and is_lock and self.command in ("GET", "HEAD") and rig.w2_hold.is_set():
                rig.release_w2()

            # Contract guards on the lock object (the lock must never be deleted, and every write
            # must be conditional). These actively defeat delete/recreate and blind-overwrite
            # shortcuts, and keep clear of the emulator's broken If-None-Match handling.
            if is_lock and self.command == "DELETE":
                rig.note_violation(f"{role}: DELETE on the lock object")
                return self._error(403, "AccessDenied", "The lock object must not be deleted")
            if is_lock and self.command == "PUT":
                if _header_ci(headers, "if-none-match") is not None:
                    rig.note_violation(f"{role}: lock PUT used If-None-Match")
                    return self._error(400, "InvalidRequest", "If-None-Match is not allowed here")
                if _header_ci(headers, "if-match") is None:
                    rig.note_violation(f"{role}: unconditional lock PUT (no If-Match)")
                    return self._error(400, "InvalidRequest", "lock writes must carry If-Match")

            # A lock-object write under the fault policy (only the faulted role).
            if (is_lock and self.command == "PUT" and rig.policy is not None
                    and role == rig.policy.role):
                return self._lock_put_faulted(path, headers, body)

            is_counter = rig.is_counter_key(path) and self.command == "PUT"
            if is_counter and rig.policy is not None and role == rig.policy.role:
                return self._counter_put_faulted(path, headers, body)

            up = forward(rig, self.command, path, headers, body)
            if is_counter and up.status == 200:
                rig.record_counter_write(role, body)

            # Record what landed / was read on the lock object for the chain and read log.
            if is_lock and self.command == "PUT" and up.status == 200:
                self._record_put(headers, body, up)
            elif is_lock and self.command in ("GET", "HEAD") and up.status == 200:
                rig.record_read(role, _etag_of(up.headers))
            return self._relay(up)

        def _record_put(self, headers, body, up):
            try:
                parsed = json.loads(body)
            except Exception:
                rig.note_violation(f"{role}: lock body is not JSON")
                parsed = {}
            if not all(k in parsed for k in REQUIRED_KEYS):
                rig.note_violation(f"{role}: lock body missing required keys: {sorted(parsed)}")
            im = _header_ci(headers, "if-match")
            return rig.record_lock_write(role, im, parsed, _etag_of(up.headers), self.recv_ms)

        def _lock_put_faulted(self, path, headers, body):
            try:
                parsed = json.loads(body)
            except Exception:
                parsed = {}
            im = _header_ci(headers, "if-match")
            decision = rig.policy.decide(im, parsed)
            if decision.action == "fail_503":
                return self._error(503, "ServiceUnavailable", "Service Unavailable")
            if decision.action == "forward":
                up = forward(rig, "PUT", path, headers, body)
                if up.status == 200:
                    w = self._record_put(headers, body, up)
                    decision.after_land(self, parsed, w)
                return self._relay(up)
            if decision.action == "land_then_503":
                up = forward(rig, "PUT", path, headers, body)
                if up.status != 200:
                    decision.after_land(self, parsed, None)   # did not land; phase re-fires
                    return self._relay(up)
                decision.after_land(self, parsed, self._record_put(headers, body, up))
                return self._error(503, "ServiceUnavailable", "Service Unavailable")
            if decision.action == "land_then_close":
                up = forward(rig, "PUT", path, headers, body)
                if up.status != 200:
                    decision.after_land(self, parsed, None)
                    return self._relay(up)
                decision.after_land(self, parsed, self._record_put(headers, body, up))
                self.close_connection = True
                return  # no response line: the client sees a dropped connection
            if decision.action == "sidecar_then_503":
                decision.before_fail(self, parsed)
                return self._error(503, "ServiceUnavailable", "Service Unavailable")
            # default: forward
            up = forward(rig, "PUT", path, headers, body)
            if up.status == 200:
                w = self._record_put(headers, body, up)
                decision.after_land(self, parsed, w)
            return self._relay(up)

        def _counter_put_faulted(self, path, headers, body):
            decision = rig.policy.decide_counter(body)
            if decision.action == "land_then_503":
                up = forward(rig, "PUT", path, headers, body)
                if up.status == 200:
                    rig.record_counter_write(role, body)
                    decision.after_land(self, body, up)
                    return self._error(503, "ServiceUnavailable", "Service Unavailable")
                return self._relay(up)
            if decision.action == "504_then_late":
                held = (path, dict(headers), body)
                t = threading.Thread(target=self._forward_late, args=(held, decision),
                                     daemon=True)
                rig.late_writes.append(t)
                t.start()
                return self._error(504, "GatewayTimeout", "Gateway Timeout")
            up = forward(rig, "PUT", path, headers, body)
            if up.status == 200:
                rig.record_counter_write(role, body)
            return self._relay(up)

        def _forward_late(self, held, decision):
            # The backend request the gateway gave up on is still running: it reaches storage
            # once the table has moved on (the next commit landed), or after a bound.
            path, headers, body = held
            try:
                held_seq = int(json.loads(body).get("seq"))
            except Exception:
                held_seq = None
            deadline = time.monotonic() + decision.late_timeout_s
            while time.monotonic() < deadline:
                cur = rig.upstream_counter_seq()
                if held_seq is not None and cur is not None and cur > held_seq:
                    break
                time.sleep(0.05)
            up = forward(rig, "PUT", path, headers, body)
            if up.status == 200:
                rig.record_counter_write(role + "-late", body)
            decision.after_late(up.status)

        do_GET = _handle
        do_PUT = _handle
        do_POST = _handle
        do_HEAD = _handle
        do_DELETE = _handle

    return Handler


def _header_ci(headers, name):
    for k, v in headers.items():
        if k.lower() == name:
            return v
    return None


def _etag_of(header_list):
    for k, v in header_list:
        if k.lower() == "etag":
            return v
    return None


def start_server(rig, role, port):
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(rig, role))
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd
