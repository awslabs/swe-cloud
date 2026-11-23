"""Backfill for the object journal.

Rewrites records still stored in the XML envelope into the JSON envelope
without touching their content: resolve the head, fetch the object, decode,
re-encode, write a fresh object, move the head conditionally. Live traffic
keeps running against the same records while a sweep is in progress.

When a control socket is configured, the sweep announces every snapshot it
has taken — after the durable head and object read, before any publication
attempt — and waits at that point for the controller's reply. With no socket
configured it runs straight through.
"""

import argparse
import json
import os
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import records
import store

MAX_ATTEMPTS = 8
MAX_BACKOFF = 0.1


class Sweep:
    def __init__(self, args):
        self.args = args
        self.store = store.Store(
            args.endpoint, args.region, args.bucket, args.table
        )
        self.control_path = args.control

    def _pause(self, facts):
        if not self.control_path:
            return
        announce = json.dumps({"event": "after_snapshot", **facts}) + "\n"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(self.control_path)
            client.sendall(announce.encode("utf-8"))
            client.recv(4096)

    def convert(self, rid):
        head = self.store.read_head(rid)
        if head is None:
            return "missing"
        if head["fmt"] == records.JSON_FORMAT:
            return "current"
        ctrl = self.store.read_ctrl()
        body = self.store.get_blob(head["blob_key"])
        if body is None:
            return "object missing"
        fmt, rec = records.decode(body, head["fmt"])
        if fmt != records.XML_FORMAT:
            return "unexpected format"

        self._pause(
            {
                "record_id": rid,
                "revision": head["revision"],
                "epoch": ctrl["epoch"],
                "blob_key": head["blob_key"],
                "barrier_id": "%s-%s" % (rid, uuid.uuid4().hex[:8]),
            }
        )

        out_body = records.encode(rec, records.JSON_FORMAT)
        blob_key = "rec/%s/%s" % (rid, uuid.uuid4().hex)
        self.store.put_blob(blob_key, out_body)
        op_id = "sweep-%s" % uuid.uuid4().hex

        receipt = self.store.publish(
            rid, blob_key, records.JSON_FORMAT,
            head["revision"], ctrl["epoch"], op_id,
        )
        attempts = 1
        while receipt is None and attempts < MAX_ATTEMPTS:
            time.sleep(MAX_BACKOFF)
            attempts += 1
            head = self.store.read_head(rid)
            ctrl = self.store.read_ctrl()
            if ctrl["ceiling"] < records.JSON_FORMAT:
                return "deferred"
            receipt = self.store.publish(
                rid, blob_key, records.JSON_FORMAT,
                head["revision"], ctrl["epoch"], op_id,
            )
        if receipt is None:
            return "conflict"
        return "converted"

    def sweep(self, record_ids):
        results = {}
        for rid in record_ids:
            try:
                results[rid] = self.convert(rid)
            except Exception as exc:
                results[rid] = "error: %s" % exc
        return results


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    sweep = None

    def log_message(self, fmt, *args):
        pass

    def _reply(self, code, doc):
        payload = json.dumps(doc).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        if self.path != "/sweep":
            self._reply(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            results = self.sweep.sweep(req.get("record_ids") or [])
            self._reply(200, {"results": results})
        except Exception as exc:
            self._reply(500, {"error": "internal: %s" % exc})


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="envelope backfill")
    parser.add_argument("--run", default="local")
    parser.add_argument("--endpoint", default=os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566"))
    parser.add_argument("--region", default=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--table", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--control", default=None)
    return parser.parse_args(argv)


def main():
    args = parse_args()
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", args.port),
        type("BoundHandler", (Handler,), {"sweep": Sweep(args)}),
    )
    print("backfill serving on %d" % args.port, flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
