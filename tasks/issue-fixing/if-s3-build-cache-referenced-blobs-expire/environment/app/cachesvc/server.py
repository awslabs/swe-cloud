"""HTTP front end. JSON bodies; digests are {"hash": <sha256 hex>, "size": <int>}.

    GET  /healthz                         200 once the service is ready
    POST /findmissing  {"digests": [...]} {"missing": [...]}
    PUT  /cas/<hash>   raw bytes          200 once cas.v2/<hh>/<hash> is in the bucket
    GET  /cas/<hash>                      bytes, or 404
    PUT  /ac/<hash>    {"outputs": [...]} 200 once ac/<hh>/<hash> is in the bucket
    GET  /ac/<hash>                       the AC entry as stored, or 404
"""

import json
import logging
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .disk import BadRequest
from .s3proxy import AC, CAS

log = logging.getLogger("cachesvc.http")

_HASH = re.compile(r"^[0-9a-f]{64}$")


def _valid_digest(d):
    return (isinstance(d, dict) and isinstance(d.get("hash"), str) and _HASH.match(d["hash"])
            and isinstance(d.get("size"), int) and d["size"] >= 0)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    cache = None  # set by make_server

    def log_message(self, fmt, *args):
        log.debug("%s " + fmt, self.address_string(), *args)

    def _reply(self, status, body=b"", content_type="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _route(self):
        parts = self.path.split("?", 1)[0].strip("/").split("/")
        if len(parts) == 2 and parts[0] in ("cas", "ac") and _HASH.match(parts[1]):
            return parts[0], parts[1]
        return None, None

    def do_GET(self):
        if self.path == "/healthz":
            return self._reply(200, {"ok": True, "disk": self.cache.stats()})
        kind, hash_ = self._route()
        if kind is None:
            return self._reply(404, {"error": "not found"})
        try:
            data = self.cache.get(AC if kind == "ac" else CAS, hash_)
        except Exception as e:  # noqa: BLE001
            log.exception("GET %s failed", self.path)
            return self._reply(502, {"error": str(e)})
        if data is None:
            return self._reply(404, {"error": "not found"})
        ctype = "application/json" if kind == "ac" else "application/octet-stream"
        return self._reply(200, data, ctype)

    def do_PUT(self):
        kind, hash_ = self._route()
        if kind is None:
            return self._reply(404, {"error": "not found"})
        data = self._body()
        if kind == "ac":
            try:
                entry = json.loads(data)
                assert isinstance(entry.get("outputs"), list)
                assert all(_valid_digest(d) for d in entry["outputs"])
            except Exception:  # noqa: BLE001
                return self._reply(400, {"error": "bad action result"})
        try:
            self.cache.put(AC if kind == "ac" else CAS, hash_, data)
        except BadRequest as e:
            return self._reply(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            log.exception("PUT %s failed", self.path)
            return self._reply(502, {"error": str(e)})
        return self._reply(200, {"ok": True})

    def do_POST(self):
        if self.path != "/findmissing":
            return self._reply(404, {"error": "not found"})
        try:
            digests = json.loads(self._body())["digests"]
            assert all(_valid_digest(d) for d in digests)
        except Exception:  # noqa: BLE001
            return self._reply(400, {"error": "bad request"})
        try:
            missing = self.cache.find_missing(digests)
        except Exception as e:  # noqa: BLE001
            log.exception("findmissing failed")
            return self._reply(502, {"error": str(e)})
        return self._reply(200, {"missing": missing})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128


def make_server(host, port, cache):
    handler = type("BoundHandler", (Handler,), {"cache": cache})
    return Server((host, port), handler)
