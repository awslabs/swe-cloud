"""Rollout coordinator for the object journal fleet.

Slot processes are launched and replaced one at a time by the operating
harness; this process owns the fleet-wide control item and answers phase
requests against it:

  prepare   idle -> prepare      both envelope readers everywhere before
                               anything else changes
  activate  prepare -> activate permits JSON publication
  rollback  activate -> prepare reinstalls the prepared build's regime

A request that cannot be applied to the current state is refused with the
observed control state rather than forced. Activation additionally requires
every slot currently registered to read both envelopes — a slot that has not
been replaced yet would be serving traffic it cannot decode. Every accepted
move bumps the control epoch, which fences publications that were still in
flight against the previous state.
"""

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import store

DUAL_READS = {1, 2}


class Deployer:
    def __init__(self, args):
        self.args = args
        self.store = store.Store(
            args.endpoint, args.region, args.bucket, args.table
        )

    def status(self):
        ctrl = self.store.read_ctrl()
        if ctrl is None:
            return {"error": "control item missing"}
        return ctrl

    def request(self, action):
        ctrl = self.store.read_ctrl()
        if ctrl is None:
            return {"status": "blocked", "reason": "control item missing"}
        phase, epoch, ceiling = ctrl["phase"], ctrl["epoch"], ctrl["ceiling"]

        if action == "prepare":
            if phase == "prepare":
                return {"status": "applied", "phase": phase, "epoch": epoch}
            if phase != "idle":
                return {"status": "blocked", "reason": "phase is %s" % phase,
                        "phase": phase, "epoch": epoch}
            ok = self.store.try_advance_ctrl("idle", epoch, "prepare", 1)
            return self._answer(ok, "prepare", epoch)

        if action == "activate":
            if phase == "activate":
                return {"status": "applied", "phase": phase, "epoch": epoch}
            if phase != "prepare":
                return {"status": "blocked", "reason": "phase is %s" % phase,
                        "phase": phase, "epoch": epoch}
            hosts = self.store.list_hosts()
            if not hosts:
                return {"status": "blocked", "reason": "no slots registered",
                        "phase": phase, "epoch": epoch}
            dual = [h for h in hosts if set(h["reads"]) >= DUAL_READS]
            if len(dual) < len(hosts):
                return {"status": "blocked", "reason": "fleet not ready",
                        "phase": phase, "epoch": epoch}
            ok = self.store.try_advance_ctrl("prepare", epoch, "activate", 2)
            return self._answer(ok, "activate", epoch)

        if action == "rollback":
            if phase == "prepare":
                return {"status": "applied", "phase": phase, "epoch": epoch}
            if phase != "activate":
                return {"status": "blocked", "reason": "phase is %s" % phase,
                        "phase": phase, "epoch": epoch}
            # The prepared build reads both envelopes, so JSON publication
            # stays readable after the move; only the epoch fence changes.
            ok = self.store.try_advance_ctrl("activate", epoch, "prepare", 2)
            return self._answer(ok, "prepare", epoch)

        return {"status": "blocked", "reason": "unknown action"}

    def _answer(self, ok, phase, epoch):
        if ok:
            return {"status": "applied", "phase": phase, "epoch": epoch + 1}
        return {"status": "blocked", "reason": "control moved",
                "phase": phase, "epoch": epoch}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    deployer = None

    def log_message(self, fmt, *args):
        pass

    def _reply(self, code, doc):
        payload = json.dumps(doc).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/status":
            self._reply(200, self.deployer.status())
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/phase":
            self._reply(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            self._reply(200, self.deployer.request(req.get("action")))
        except Exception as exc:
            self._reply(500, {"error": "internal: %s" % exc})


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="rollout coordinator")
    parser.add_argument("--run", default="local")
    parser.add_argument("--endpoint", default=os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566"))
    parser.add_argument("--region", default=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--table", required=True)
    parser.add_argument("--port", type=int, required=True)
    return parser.parse_args(argv)


def main():
    args = parse_args()
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", args.port),
        type("BoundHandler", (Handler,), {"deployer": Deployer(args)}),
    )
    print("coordinator serving on %d" % args.port, flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
