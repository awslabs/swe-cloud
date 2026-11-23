"""The HTTP/JSON control API.

    POST /open   {"doc": name}             200 once the document is loaded and the connection counted
    POST /edit   {"doc": name, "edit_id"}  200 once the edit is applied in memory (the acknowledgement)
    POST /close  {"doc": name}             202 immediately; the last close stores and unloads
    GET  /docs/{name}                      {"loaded": bool, "connections": int, ...}
    GET  /healthz                          200 while the process is up
"""

from __future__ import annotations

import json
import logging
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .server import DocServer, NotOpen

log = logging.getLogger("docserver.http")
NAME = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")


def make_handler(server: DocServer):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # keep stderr for the application log
            pass

        def _reply(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length) or b"{}")

        def do_GET(self):
            if self.path == "/healthz":
                return self._reply(200, {"ok": True, "instance": server.instance_id})
            if self.path.startswith("/docs/"):
                name = self.path[len("/docs/") :]
                if not NAME.match(name):
                    return self._reply(400, {"error": "bad document name"})
                return self._reply(200, server.status(name))
            return self._reply(404, {"error": "not found"})

        def do_POST(self):
            try:
                body = self._body()
            except ValueError:
                return self._reply(400, {"error": "bad json"})
            name = body.get("doc")
            if not isinstance(name, str) or not NAME.match(name):
                return self._reply(400, {"error": "bad document name"})
            try:
                if self.path == "/open":
                    document = server.open(name)
                    return self._reply(200, {"loaded": True, "connections": document.connections})
                if self.path == "/edit":
                    edit_id = body.get("edit_id")
                    if not isinstance(edit_id, str) or not edit_id:
                        return self._reply(400, {"error": "bad edit_id"})
                    server.edit(name, edit_id)
                    return self._reply(200, {"ok": True})
                if self.path == "/close":
                    server.close(name)
                    return self._reply(202, {"ok": True})
            except NotOpen:
                return self._reply(409, {"error": f"{name} has no open connection"})
            except Exception as error:
                log.exception("%s %s failed", self.path, name)
                return self._reply(500, {"error": repr(error)})
            return self._reply(404, {"error": "not found"})

    return Handler


def serve(server: DocServer, port: int) -> None:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(server))
    httpd.daemon_threads = True
    log.info("instance %s listening on 127.0.0.1:%d", server.instance_id, port)
    httpd.serve_forever()
