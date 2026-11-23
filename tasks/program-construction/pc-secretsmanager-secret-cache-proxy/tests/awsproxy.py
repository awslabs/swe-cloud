"""A recording, fault-injecting HTTP forwarder placed between the program and the AWS emulator."""

import http.client
import http.server
import json
import re
import socket
import threading
import urllib.parse

UPSTREAM = ("engines", 4566)


def operation(headers, path, body):
    """Name the AWS operation of a request: Secrets Manager ops bare, STS ops prefixed with 'sts:'."""
    target = headers.get("X-Amz-Target")
    if target:
        service, _, op = target.rpartition(".")
        return ("sts:" + op if "SecurityToken" in service else op), _json(body)
    match = re.match(r"^/service/([^/]+)/operation/([^/?]+)", path)
    if match:
        return ("sts:" if "SecurityToken" in match.group(1) else "") + match.group(2), {}
    form = dict(urllib.parse.parse_qsl(body.decode("latin-1")))
    if "Action" in form:
        return "sts:" + form["Action"], form
    return "?", {}


def _json(body):
    try:
        value = json.loads(body or b"{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


class Recorder:
    """Forward every request to the emulator unless `rule(op, params)` returns a canned (status, body)."""

    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()
        self.rule = None
        self.connections = set()
        recorder = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def setup(self):
                super().setup()
                with recorder.lock:
                    recorder.connections.add(self.connection)

            def finish(self):
                with recorder.lock:
                    recorder.connections.discard(self.connection)
                super().finish()

            def handle_request(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                op, params = operation(self.headers, self.path, body)
                with recorder.lock:
                    recorder.calls.append((op, params))
                    rule = recorder.rule
                verdict = rule(op, params) if rule else None
                if verdict is not None:
                    status, payload = verdict
                    data = payload.encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "text/xml" if payload.startswith("<") else "application/x-amz-json-1.1")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                headers = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "connection", "content-length", "transfer-encoding")}
                headers["Host"] = "%s:%d" % UPSTREAM
                upstream = http.client.HTTPConnection(*UPSTREAM, timeout=30)
                try:
                    upstream.request(self.command, self.path, body=body, headers=headers)
                    response = upstream.getresponse()
                    data = response.read()
                    self.send_response(response.status)
                    for key, value in response.getheaders():
                        if key.lower() not in ("connection", "content-length", "transfer-encoding", "server", "date"):
                            self.send_header(key, value)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                finally:
                    upstream.close()

            do_GET = do_POST = do_PUT = do_DELETE = handle_request

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.closed = False
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def take(self):
        """Operation names recorded since the previous call."""
        with self.lock:
            calls, self.calls = self.calls, []
        return [op for op, _ in calls]

    def take_calls(self):
        with self.lock:
            calls, self.calls = self.calls, []
        return calls

    def close(self):
        """Stop listening and sever open connections: later requests to the endpoint are refused."""
        if not self.closed:
            self.closed = True
            self.server.shutdown()
            self.server.server_close()
            with self.lock:
                open_connections = list(self.connections)
            for connection in open_connections:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
