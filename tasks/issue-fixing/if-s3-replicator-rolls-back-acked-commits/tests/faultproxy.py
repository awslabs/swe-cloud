"""S3 reverse proxy in front of the emulator that can hold or fail PUTs by key.

Every request is forwarded byte-for-byte (Host header included, so the client's SigV4 signature
stays valid) unless an armed rule matches. A held PUT is read in full, never forwarded and never
answered with success: it gets a 503 when its rule is cleared or after HOLD_MAX_S, whichever is
first, by which time the client is normally dead.

A throttle burst answers every PUT with 503 SlowDown (not forwarded) for its duration, except the
first PUT matching its `lose` pattern: that one is forwarded, stored by the emulator, and answered
504 Gateway Timeout, as the production gateway does when the store is slow to reply.
"""
import hashlib
import http.client
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

HOLD_MAX_S = 60.0

HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
              "trailers", "transfer-encoding", "upgrade", "expect"}

SLOWDOWN = (b'<?xml version="1.0" encoding="UTF-8"?>\n<Error><Code>SlowDown</Code>'
            b'<Message>Please reduce your request rate.</Message><RequestId>faultproxy</RequestId>'
            b'</Error>')

GATEWAY_TIMEOUT = (b'<?xml version="1.0" encoding="UTF-8"?>\n<Error><Code>GatewayTimeout</Code>'
                   b'<Message>The gateway timed out waiting for the store.</Message>'
                   b'<RequestId>faultproxy</RequestId></Error>')


class FaultProxy:
    def __init__(self, upstream_port, listen_port):
        self.upstream_port = upstream_port
        self.port = listen_port
        self.lock = threading.Lock()
        self.rules = []
        self.release = threading.Event()
        self.held = []      # {"key", "ts", "active"}
        # {"ts", "method", "bucket", "key", "status", "upstream", "sha", "bytes", "query", "held"}
        # status: what the client got; upstream: the emulator's status if forwarded, else None
        self.log = []
        self.burst = None   # {"until", "lose": regex or None, "lost": [keys]}
        self.server = None

    # ---------------------------------------------------------------- control
    def arm_hold(self, prefix_regex, exclude_regex):
        with self.lock:
            self.rules.append({"type": "hold", "re": re.compile(prefix_regex),
                               "exclude": re.compile(exclude_regex)})

    def arm_fail(self, key_regex, count, status=503):
        with self.lock:
            self.rules.append({"type": "fail", "re": re.compile(key_regex), "count": count,
                               "status": status})

    def arm_burst(self, seconds, lose_regex=None):
        with self.lock:
            self.burst = {"until": time.time() + seconds,
                          "lose": re.compile(lose_regex) if lose_regex else None, "lost": []}

    def burst_info(self):
        with self.lock:
            return dict(self.burst) if self.burst else None

    def release_holds(self):
        """Drops the hold rules; requests held so far are answered 503."""
        with self.lock:
            self.rules = [r for r in self.rules if r["type"] != "hold"]
            release, self.release = self.release, threading.Event()
        release.set()

    def clear_rules(self):
        with self.lock:
            self.rules = []
            self.burst = None
            release, self.release = self.release, threading.Event()
        release.set()

    def held_keys(self, active_only=True):
        with self.lock:
            return [dict(h) for h in self.held if h["active"] or not active_only]

    def get_log(self):
        with self.lock:
            return [dict(e) for e in self.log]

    def _record(self, **entry):
        entry.setdefault("ts", time.time())
        with self.lock:
            self.log.append(entry)

    def _decide(self, method, key):
        """Returns ("forward"|"fail"|"lose"|"hold", rule)."""
        if method != "PUT" or not key:
            return "forward", None
        with self.lock:
            b = self.burst
            if b and time.time() < b["until"]:
                if b["lose"] is not None and b["lose"].search(key):
                    b["lose"] = None
                    b["lost"].append(key)
                    return "lose", None
                return "fail", {"status": 503}
            for rule in self.rules:
                if rule["type"] == "fail" and rule["count"] > 0 and rule["re"].search(key):
                    rule["count"] -= 1
                    return "fail", rule
            for rule in self.rules:
                if (rule["type"] == "hold" and rule["re"].search(key)
                        and not rule["exclude"].search(key)):
                    return "hold", self.release
        return "forward", None

    # ---------------------------------------------------------------- server
    def start(self):
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _read_body(self):
                if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                    chunks = []
                    while True:
                        size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                        if size == 0:
                            while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                                pass
                            break
                        chunks.append(self.rfile.read(size))
                        self.rfile.readline()
                    return b"".join(chunks), True
                n = int(self.headers.get("Content-Length") or 0)
                return (self.rfile.read(n) if n else b""), False

            def _send(self, status, headers, body, head=False):
                self.send_response(status)
                sent_len = False
                for k, v in headers:
                    lk = k.lower()
                    if lk in HOP_BY_HOP:
                        continue
                    if lk == "content-length":
                        if not head:
                            continue
                        sent_len = True
                    self.send_header(k, v)
                if not head or not sent_len:
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if not head and body:
                    self.wfile.write(body)

            def _handle(self):
                method = self.command
                parts = urlsplit(self.path)
                path = unquote(parts.path).lstrip("/")
                bucket, _, key = path.partition("/")
                body, dechunked = self._read_body()
                decision, rule = proxy._decide(method, key)
                entry = {"method": method, "bucket": bucket, "key": key or None,
                         "query": parts.query, "bytes": len(body), "held": False,
                         "upstream": None,
                         "sha": hashlib.sha256(body).hexdigest() if method == "PUT" else None}
                if decision == "fail":
                    proxy._record(status=rule["status"], **entry)
                    self._send(rule["status"], [("Content-Type", "application/xml")], SLOWDOWN)
                    return
                if decision == "hold":
                    h = {"key": key, "ts": time.time(), "active": True}
                    with proxy.lock:
                        proxy.held.append(h)
                    entry["held"] = True
                    rule.wait(HOLD_MAX_S)
                    with proxy.lock:
                        h["active"] = False
                    proxy._record(status=503, **entry)
                    try:
                        self._send(503, [("Content-Type", "application/xml")], SLOWDOWN)
                    except OSError:
                        pass
                    self.close_connection = True
                    return
                headers = {}
                for k, v in self.headers.items():
                    if k.lower() in HOP_BY_HOP:
                        continue
                    headers[k] = v
                if dechunked or method in ("PUT", "POST"):
                    headers = {k: v for k, v in headers.items() if k.lower() != "content-length"}
                    headers["Content-Length"] = str(len(body))
                conn = http.client.HTTPConnection("127.0.0.1", proxy.upstream_port, timeout=120)
                try:
                    conn.request(method, self.path, body=body if body or method in ("PUT", "POST")
                                 else None, headers=headers)
                    resp = conn.getresponse()
                    data = resp.read()
                    status, rheaders = resp.status, resp.getheaders()
                finally:
                    conn.close()
                entry["upstream"] = status
                if decision == "lose":
                    proxy._record(status=504, **entry)
                    self._send(504, [("Content-Type", "application/xml")], GATEWAY_TIMEOUT)
                    return
                proxy._record(status=status, **entry)
                self._send(status, rheaders, data, head=(method == "HEAD"))

            def do_GET(self):
                self._handle()

            do_PUT = do_POST = do_DELETE = do_HEAD = do_GET

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1},
                         daemon=True).start()
        return self

    def stop(self):
        self.clear_rules()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
