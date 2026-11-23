#!/usr/bin/env python3
"""relay -- the operator's front door to Route 53 in this environment.

Every Route 53 request from the reconciler goes through this relay. It does two
things and nothing else:

  * it forwards the request to the emulator unchanged;
  * it refuses a ChangeResourceRecordSets request whose batch carries more
    changes than the per-call change limit.

The reconciler must not try to talk to anything but this relay.

State lives under /run/r53relay/:
  cycle        a plain integer naming the current reconcile cycle, for the log
  calls.jsonl  one JSON line per ChangeResourceRecordSets request
"""

import http.client
import json
import os
import threading
import time
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LISTEN_PORT = 4567
UPSTREAM_HOST = "127.0.0.1"
UPSTREAM_PORT = 4566
RUN_DIR = "/run/r53relay"
CYCLE_PATH = os.path.join(RUN_DIR, "cycle")
LOG_PATH = os.path.join(RUN_DIR, "calls.jsonl")

CHANGE_LIMIT = 8
NS = "{https://route53.amazonaws.com/doc/2013-04-01/}"

_lock = threading.Lock()


def _normalise(name):
    name = (name or "").strip().lower()
    return name[:-1] if name.endswith(".") else name


def current_cycle():
    try:
        return int(open(CYCLE_PATH).read().strip())
    except (OSError, ValueError):
        return 0


def _log(record):
    line = json.dumps(record, sort_keys=True) + "\n"
    with _lock:
        with open(LOG_PATH, "a") as handle:
            handle.write(line)


def parse_batch(body):
    """Return [ {action,name,type,values} ] or []."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []
    items = []
    for change in root.iter(NS + "Change"):
        action = change.findtext(NS + "Action") or ""
        rrs = change.find(NS + "ResourceRecordSet")
        if rrs is None:
            continue
        name = rrs.findtext(NS + "Name") or ""
        rtype = rrs.findtext(NS + "Type") or ""
        values = [
            (v.text or "")
            for v in rrs.iter(NS + "Value")
        ]
        items.append(
            {"action": action.strip(), "name": _normalise(name), "type": rtype.strip(), "values": values}
        )
    return items


def _error_body(code, message):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<ErrorResponse xmlns="https://route53.amazonaws.com/doc/2013-04-01/">'
        "<Error><Type>Sender</Type><Code>%s</Code><Message>%s</Message></Error>"
        "<RequestId>relay-%d</RequestId></ErrorResponse>" % (code, message, int(time.time()))
    )


HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-length",
}


class Relay(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "r53relay/1.0"

    def log_message(self, fmt, *args):
        pass

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _reply(self, status, body, content_type="text/xml"):
        payload = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _forward(self, method, path, body):
        headers = {
            k: v
            for k, v in self.headers.items()
            if k.lower() not in HOP_HEADERS
        }
        headers["Accept-Encoding"] = "identity"
        if body:
            headers["Content-Length"] = str(len(body))
        conn = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=30)
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            conn.close()

    def do_GET(self):
        if self.path == "/_health":
            self._reply(200, "ok", "text/plain")
            return
        body = self._read_body()
        status, payload, _ = self._forward("GET", self.path, body)
        self._reply(status, payload)

    def do_POST(self):
        body = self._read_body()
        path = self.path
        if "/rrset" not in path:
            status, payload, _ = self._forward("POST", path, body)
            self._reply(status, payload)
            return

        items = parse_batch(body)
        cycle = current_cycle()

        if len(items) > CHANGE_LIMIT:
            message = (
                "FATAL problem: the change batch carries %d changes and this "
                "endpoint accepts at most %d changes per ChangeResourceRecordSets "
                "request" % (len(items), CHANGE_LIMIT)
            )
            _log(
                {
                    "cycle": cycle,
                    "changes": len(items),
                    "items": items,
                    "accepted": False,
                    "status": 400,
                    "error": message,
                }
            )
            self._reply(400, _error_body("InvalidChangeBatch", message))
            return

        status, payload, _ = self._forward("POST", path, body)
        accepted = 200 <= status < 300
        _log(
            {
                "cycle": cycle,
                "changes": len(items),
                "items": items,
                "accepted": accepted,
                "status": status,
                "error": None if accepted else payload.decode("utf-8", "replace")[:600],
            }
        )
        self._reply(status, payload)


def main():
    os.makedirs(RUN_DIR, exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Relay)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
