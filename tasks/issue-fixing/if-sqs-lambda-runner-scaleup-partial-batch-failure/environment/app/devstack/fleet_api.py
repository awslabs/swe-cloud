#!/usr/bin/env python3
"""Fleet API for local development: the EC2 front the scale-up function talks to.

    python3 devstack/fleet_api.py [--port 8081]

Same endpoints and answers as the production fleet API (README.md, "Fleet API"); instances live in
memory and "launch" instantly. Like production, describe-runners lists a new instance only a few
seconds after its launch. GET /instances lists every instance, terminated ones included.
"""

from __future__ import annotations

import argparse
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOCK = threading.Lock()
INSTANCES: dict[str, dict] = {}
DESCRIBE_LAG_S = 7.0


def describe_runners(body: dict) -> tuple[int, dict]:
    now = time.time()
    with LOCK:
        found = [
            {key: value for key, value in item.items() if not key.startswith("_")}
            for item in INSTANCES.values()
            if item["state"] in ("pending", "running")
            and now >= item["_launched_at"] + DESCRIBE_LAG_S
            and item["owner"] == body.get("owner")
            and item["environment"] == body.get("environment", item["environment"])
        ]
    return 200, {"instances": found}


def create_fleet(body: dict) -> tuple[int, dict]:
    job_ids = body.get("job_ids") or []
    if not isinstance(job_ids, list) or not body.get("owner"):
        return 400, {"__type": "InvalidParameterValue", "message": "owner and job_ids are required"}
    launched = []
    with LOCK:
        for job_id in job_ids:
            instance_id = "i-" + secrets.token_hex(9)[:17]
            INSTANCES[instance_id] = {
                "instance_id": instance_id,
                "environment": body.get("environment", ""),
                "owner": body["owner"],
                "job_id": job_id,
                "state": "running",
                "launch_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "_launched_at": time.time(),
            }
            launched.append({"instance_id": instance_id, "job_id": job_id})
    return 200, {"instances": launched, "errors": []}


def terminate(body: dict) -> tuple[int, dict]:
    with LOCK:
        item = INSTANCES.get(body.get("instance_id", ""))
        if item is None:
            return 400, {"__type": "InvalidInstanceID.NotFound", "message": f"The instance ID '{body.get('instance_id')}' does not exist"}
        item["state"] = "terminated"
    return 200, {}


ROUTES = {"/describe-runners": describe_runners, "/create-fleet": create_fleet, "/terminate": terminate}


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
            return self._send(400, {"__type": "MalformedRequest", "message": "body is not JSON"})
        route = ROUTES.get(self.path)
        if route is None:
            return self._send(404, {"__type": "UnknownOperation", "message": self.path})
        self._send(*route(body))

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/instances":
            return self._send(404, {"__type": "UnknownOperation", "message": self.path})
        with LOCK:
            self._send(200, {"instances": [{k: v for k, v in item.items() if not k.startswith("_")} for item in INSTANCES.values()]})

    def log_message(self, fmt: str, *args) -> None:
        print(f"{time.strftime('%H:%M:%S')} fleet {self.command} {self.path} {fmt % args}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8081)
    ThreadingHTTPServer(("127.0.0.1", parser.parse_args().port), Handler).serve_forever()
