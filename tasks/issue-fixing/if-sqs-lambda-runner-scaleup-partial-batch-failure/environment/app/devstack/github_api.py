#!/usr/bin/env python3
"""Code-host API for local development: the JIT runner-config endpoint the scale-up function calls.

    python3 devstack/github_api.py [--port 8082]

POST /orgs/{org}/actions/runners/generate-jitconfig {name, runner_group_id, labels}
  -> 201 {"runner": {"id", "name", "labels"}, "encoded_jit_config"}
GET /runners lists every runner registered so far.
"""

from __future__ import annotations

import argparse
import base64
import itertools
import json
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOCK = threading.Lock()
RUNNER_IDS = itertools.count(1000)
RUNNERS: list[dict] = []
JIT_PATH = re.compile(r"^/orgs/([^/]+)/actions/runners/generate-jitconfig$")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, {"message": "Problems parsing JSON"})
        match = JIT_PATH.match(self.path)
        if not match:
            return self._send(404, {"message": "Not Found"})
        if not body.get("name"):
            return self._send(422, {"message": "name is required"})
        with LOCK:
            runner = {"id": next(RUNNER_IDS), "name": body["name"], "labels": body.get("labels", []), "org": match.group(1)}
            encoded = base64.b64encode(json.dumps({**runner, "nonce": secrets.token_hex(8)}).encode()).decode()
            RUNNERS.append({**runner, "encoded_jit_config": encoded})
        self._send(201, {"runner": runner, "encoded_jit_config": encoded})

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/runners":
            return self._send(404, {"message": "Not Found"})
        with LOCK:
            self._send(200, {"runners": RUNNERS})

    def log_message(self, fmt: str, *args) -> None:
        print(f"{time.strftime('%H:%M:%S')} github {self.command} {self.path} {fmt % args}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8082)
    ThreadingHTTPServer(("127.0.0.1", parser.parse_args().port), Handler).serve_forever()
