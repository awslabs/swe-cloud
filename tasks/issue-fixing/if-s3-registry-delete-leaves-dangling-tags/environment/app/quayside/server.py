"""HTTP front end."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import re
from urllib.parse import urlsplit

from . import errors
from .handlers import Response, _json

log = logging.getLogger("quayside.server")

NAME = r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*"
TAG = r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}"
DIGEST = r"sha256:[0-9a-f]{64}"

MANIFEST_ROUTE = re.compile(rf"^/v2/(?P<name>{NAME})/manifests/(?P<ref>{TAG}|{DIGEST})$")
TAGS_ROUTE = re.compile(rf"^/v2/(?P<name>{NAME})/tags/list$")

MAX_MANIFEST = 4 << 20


def make_handler(registry):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "quayside"

        def log_message(self, fmt, *args):
            log.info("%s %s", self.address_string(), fmt % args)

        def _send(self, resp):
            self.send_response(resp.status)
            for k, v in resp.headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(resp.body)))
            self.end_headers()
            if resp.body and self.command != "HEAD":
                self.wfile.write(resp.body)

        def _error(self, err):
            self._send(Response(err.status, json.dumps(err.body()).encode(),
                                {"Content-Type": "application/json"}))

        def _dispatch(self):
            path = urlsplit(self.path).path
            if path == "/healthz":
                return _json(200, {"replica": registry.replica_id})
            if path in ("/v2", "/v2/"):
                return _json(200, {})
            m = MANIFEST_ROUTE.match(path)
            if m:
                name, ref = m.group("name"), m.group("ref")
                if self.command in ("GET", "HEAD"):
                    return registry.get_manifest(name, ref)
                if self.command == "PUT":
                    length = int(self.headers.get("Content-Length") or 0)
                    if length > MAX_MANIFEST:
                        raise errors.manifest_invalid("manifest too large")
                    body = self.rfile.read(length)
                    return registry.put_manifest(name, ref, body, self.headers.get("Content-Type"))
                if self.command == "DELETE":
                    return registry.delete_manifest(name, ref)
            m = TAGS_ROUTE.match(path)
            if m and self.command == "GET":
                return registry.tags_list(m.group("name"))
            raise errors.APIError(404, "NOT_FOUND", "not found")

        def _handle(self):
            try:
                resp = self._dispatch()
            except errors.APIError as err:
                self._error(err)
                return
            except Exception as exc:
                log.exception("%s %s", self.command, self.path)
                self._error(errors.unknown(str(exc)))
                return
            self._send(resp)

        do_GET = do_HEAD = do_PUT = do_DELETE = _handle

    return Handler


def serve(registry, host, port):
    httpd = ThreadingHTTPServer((host, port), make_handler(registry))
    httpd.daemon_threads = True
    log.info("replica %s listening on %s:%d", registry.replica_id, host, port)
    httpd.serve_forever()
