"""S3 fault proxy used by the verifier.

Each replica gets its own route (port). Requests are forwarded to the emulator on a fresh upstream
connection, so one replica's connection state never affects another. Rules can hold a request
until released (or dropped, after the replica was killed) or answer it with a 500.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import re
import socket
import threading
import time
from urllib.parse import unquote, urlsplit

MUTATING = ("PUT", "DELETE", "POST")
_KEY_RE = re.compile(rb"<Key>(.*?)</Key>", re.S)
_HOP = {"connection", "keep-alive", "proxy-connection", "transfer-encoding", "te", "upgrade",
        "content-length"}

FAIL_500 = (b'<?xml version="1.0" encoding="UTF-8"?>\n<Error><Code>InternalError</Code>'
            b'<Message>We encountered an internal error. Please try again.</Message></Error>')


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Held:
    def __init__(self, route, method, keys):
        self.route = route
        self.method = method
        self.keys = keys
        self._event = threading.Event()
        self.decision = None

    def release(self):
        self.decision = "forward"
        self._event.set()

    def drop(self):
        self.decision = "drop"
        self._event.set()

    def wait(self, timeout):
        if not self._event.wait(timeout):
            self.decision = "forward"


class Rule:
    """Match requests on route whose method is in methods and one of whose object keys matches
    key_re; act on the nth..last matching request (1-based, last=None means only nth,
    last=0 means every one from nth on)."""

    def __init__(self, route, action, methods=None, key_re=None, nth=1, last=None):
        self.route = route
        self.action = action
        self.methods = set(methods) if methods else None
        self.key_re = re.compile(key_re) if key_re else None
        self.nth = nth
        self.last = nth if last is None else last
        self.count = 0
        self.fired = threading.Event()
        self.held = []
        self.active = True

    def matches(self, route, method, keys):
        if not self.active or route != self.route:
            return False
        if self.methods is not None and method not in self.methods:
            return False
        if self.key_re is not None and not any(self.key_re.search(k) for k in keys):
            return False
        return True

    def take(self):
        self.count += 1
        if self.count < self.nth:
            return False
        if self.last and self.count > self.last:
            return False
        return True

    def wait_fired(self, timeout):
        return self.fired.wait(timeout)


class FaultProxy:
    def __init__(self, upstream_host, upstream_port, bucket):
        self.upstream = (upstream_host, upstream_port)
        self.bucket = bucket
        self.rules = []
        self.lock = threading.Lock()
        self.log = []
        self.violations = []
        self.servers = {}
        self.ports = {}

    # -- rules -----------------------------------------------------------------------------
    def add(self, rule):
        with self.lock:
            self.rules.append(rule)
        return rule

    def remove(self, rule):
        with self.lock:
            rule.active = False
            if rule in self.rules:
                self.rules.remove(rule)

    def clear(self):
        with self.lock:
            for r in self.rules:
                r.active = False
            self.rules = []

    def release_all(self, rule):
        for h in list(rule.held):
            h.release()

    def drop_all(self, rule):
        for h in list(rule.held):
            h.drop()

    def requests(self, route):
        with self.lock:
            return [e for e in self.log if e["route"] == route]

    # -- servers ---------------------------------------------------------------------------
    def start_route(self, route):
        port = free_port()
        server = ThreadingHTTPServer(("127.0.0.1", port), self._handler(route))
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.servers[route] = server
        self.ports[route] = port
        return f"http://127.0.0.1:{port}"

    def stop(self):
        for rule in list(self.rules):
            self.release_all(rule)
        for server in self.servers.values():
            server.shutdown()
            server.server_close()

    def _decide(self, route, method, keys):
        with self.lock:
            for rule in self.rules:
                if rule.matches(route, method, keys) and rule.take():
                    held = None
                    if rule.action == "hold":
                        held = Held(route, method, keys)
                        rule.held.append(held)
                    rule.fired.set()
                    return rule, held
        return None, None

    def _handler(self, route):
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _body(self):
                if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                    chunks = []
                    while True:
                        size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                        if size == 0:
                            while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                                pass
                            break
                        chunks.append(self.rfile.read(size))
                        self.rfile.readline()
                    return b"".join(chunks)
                length = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(length) if length else b""

            def _reply(self, status, body, headers=()):
                self.send_response(status)
                for k, v in headers:
                    self.send_header(k, v)
                if self.command != "HEAD" or not any(k.lower() == "content-length"
                                                     for k, _ in headers):
                    self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                if body and self.command != "HEAD":
                    self.wfile.write(body)
                self.close_connection = True

            def _handle(self):
                method = self.command
                body = self._body()
                parts = urlsplit(self.path)
                segs = parts.path.lstrip("/").split("/", 1)
                bucket = unquote(segs[0]) if segs and segs[0] else ""
                key = unquote(segs[1]) if len(segs) > 1 else ""
                entry = {"route": route, "method": method, "path": self.path, "t": time.time()}
                if bucket != proxy.bucket:
                    entry["status"] = 400
                    with proxy.lock:
                        proxy.violations.append(entry)
                        proxy.log.append(entry)
                    self._reply(400, b"<Error><Code>InvalidRequest</Code>"
                                     b"<Message>only the registry bucket is reachable</Message>"
                                     b"</Error>", [("Content-Type", "application/xml")])
                    return
                if method == "POST" and "delete" in parts.query:
                    keys = [unquote(k.decode()) for k in _KEY_RE.findall(body)]
                else:
                    keys = [key]
                entry["keys"] = keys
                rule, held = proxy._decide(route, method, keys)
                if rule is not None:
                    entry["rule"] = rule.action
                if held is not None:
                    held.wait(300)
                    if held.decision == "drop":
                        entry["status"] = "dropped"
                        with proxy.lock:
                            proxy.log.append(entry)
                        self.close_connection = True
                        try:
                            self.connection.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                        return
                elif rule is not None and rule.action == "fail500":
                    entry["status"] = 500
                    with proxy.lock:
                        proxy.log.append(entry)
                    self._reply(500, FAIL_500, [("Content-Type", "application/xml")])
                    return
                elif rule is not None and rule.action == "drop":
                    entry["status"] = "dropped"
                    with proxy.lock:
                        proxy.log.append(entry)
                    self.close_connection = True
                    return
                headers = {k: v for k, v in self.headers.items() if k.lower() not in _HOP}
                headers["Content-Length"] = str(len(body))
                conn = http.client.HTTPConnection(*proxy.upstream, timeout=120)
                try:
                    conn.request(method, self.path, body=body, headers=headers)
                    resp = conn.getresponse()
                    data = resp.read()
                    out = [(k, v) for k, v in resp.getheaders() if k.lower() not in _HOP]
                    if method == "HEAD":
                        cl = resp.getheader("Content-Length")
                        if cl is not None:
                            out.append(("Content-Length", cl))
                    status = resp.status
                finally:
                    conn.close()
                entry["status"] = status
                with proxy.lock:
                    proxy.log.append(entry)
                try:
                    self._reply(status, data, out)
                except OSError:
                    pass

            do_GET = do_PUT = do_POST = do_DELETE = do_HEAD = _handle

        return Handler
