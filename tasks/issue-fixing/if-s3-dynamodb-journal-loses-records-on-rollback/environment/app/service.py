"""One slot of the object journal fleet.

The process serves reads and read-modify-write updates against the shared
objects: it resolves the record's head in DynamoDB, fetches the object the
head names, decodes it with this build's readable formats, applies the
requested change, writes a fresh object and moves the head conditionally.

Every build has a fixed profile -- which envelope formats it reads, which one
it writes, which operation kinds it understands -- supplied by its launcher.
"""

import argparse
import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import records
import store

MAX_ATTEMPTS = 8
MAX_BACKOFF = 0.1


class Slot:
    def __init__(self, args, profile):
        self.args = args
        self.profile = profile
        self.store = store.Store(
            args.endpoint, args.region, args.bucket, args.table
        )

    # -- view -------------------------------------------------------------
    def ready(self):
        return {
            "build": self.profile["build"],
            "slot": self.args.slot,
            "generation": self.args.generation,
            "run": self.args.run,
            "reads": list(self.profile["reads"]),
            "writes": self.profile["writes"],
            "kinds": list(self.profile["kinds"]),
        }

    def register(self):
        self.store.put_host(
            self.args.slot,
            self.profile["build"],
            self.args.generation,
            self.profile["reads"],
            self.profile["writes"],
            self.args.run,
        )

    # -- operations ---------------------------------------------------------
    def read_record(self, rid):
        head = self.store.read_head(rid)
        if head is None:
            return None, "no such record"
        body = self.store.get_blob(head["blob_key"])
        if body is None:
            return None, "object missing"
        try:
            fmt, rec = records.decode(body, head["fmt"])
        except records.RecordError as exc:
            return None, "record unreadable: %s" % exc
        if fmt not in self.profile["reads"]:
            return None, "build does not read format %d" % fmt
        return {
            "record_id": rid,
            "revision": head["revision"],
            "counter": rec.counter,
            "format_version": fmt,
        }, None

    def decode_probe(self, body, fmt):
        if fmt not in self.profile["reads"]:
            return None, "build does not read format %d" % fmt
        try:
            got_fmt, rec = records.decode(body, fmt)
        except records.RecordError as exc:
            return None, "record unreadable: %s" % exc
        return {"format_version": got_fmt, "counter": rec.counter}, None

    def _apply(self, rec, kind, arguments):
        if kind == "increment":
            delta = arguments.get("delta", 1)
            if not isinstance(delta, int) or isinstance(delta, bool):
                return None, "delta must be an integer"
            rec.counter += delta
            return rec, None
        if kind == "set_attr":
            name = arguments.get("name")
            value = arguments.get("value")
            if not isinstance(name, str) or not name:
                return None, "attribute needs a name"
            rec.attrs[name] = value
            return rec, None
        return None, "unsupported operation kind"

    def mutate(self, rid, op_id, kind, arguments):
        if kind not in self.profile["kinds"]:
            return None, "build does not perform %s" % kind

        existing = self.store.find_op(op_id)
        if existing is not None:
            return {
                "op_id": op_id,
                "revision": existing["revision"],
                "blob_key": existing["blob_key"],
                "replayed": True,
            }, None

        head = self.store.read_head(rid)
        if head is None:
            return None, "no such record"
        ctrl = self.store.read_ctrl()
        body = self.store.get_blob(head["blob_key"])
        if body is None:
            return None, "object missing"
        in_fmt, rec = records.decode(body, head["fmt"])
        if in_fmt not in self.profile["reads"]:
            return None, "build does not read format %d" % in_fmt

        updated, err = self._apply(rec, kind, arguments)
        if err:
            return None, err
        out_fmt = self.profile["writes"]
        out_body = records.encode(updated, out_fmt)

        blob_key = "rec/%s/%s" % (rid, uuid.uuid4().hex)
        self.store.put_blob(blob_key, out_body)

        receipt = self.store.publish(
            rid, blob_key, out_fmt, head["revision"], ctrl["epoch"], op_id
        )
        attempts = 1
        while receipt is None and attempts < MAX_ATTEMPTS:
            time.sleep(MAX_BACKOFF)
            attempts += 1
            head = self.store.read_head(rid)
            ctrl = self.store.read_ctrl()
            receipt = self.store.publish(
                rid, blob_key, out_fmt, head["revision"], ctrl["epoch"], op_id
            )
        if receipt is None:
            return None, "publication rejected after %d attempts" % attempts
        receipt["op_id"] = op_id
        return receipt, None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    slot = None

    def log_message(self, fmt, *args):
        pass

    def _reply(self, code, doc):
        payload = json.dumps(doc).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def do_GET(self):
        if self.path == "/ready":
            self._reply(200, self.slot.ready())
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self):
        try:
            if self.path == "/read":
                req = self._body()
                rid = req.get("record_id")
                result, err = self.slot.read_record(rid)
                if err:
                    self._reply(404, {"error": err})
                else:
                    self._reply(200, result)
            elif self.path == "/mutate":
                req = self._body()
                result, err = self.slot.mutate(
                    req.get("record_id"),
                    req.get("op_id"),
                    req.get("kind"),
                    req.get("arguments") or {},
                )
                if err:
                    self._reply(409, {"error": err, "op_id": req.get("op_id")})
                else:
                    self._reply(200, result)
            elif self.path == "/decode":
                req = self._body()
                result, err = self.slot.decode_probe(
                    req.get("body") or "", req.get("format_version")
                )
                if err:
                    self._reply(409, {"error": err})
                else:
                    self._reply(200, result)
            else:
                self._reply(404, {"error": "not found"})
        except records.RecordError as exc:
            self._reply(409, {"error": "record unreadable: %s" % exc})
        except Exception as exc:  # keep the slot serving
            self._reply(500, {"error": "internal: %s" % exc})


def serve(args, profile):
    slot = Slot(args, profile)
    slot.register()
    handler = type(
        "BoundHandler",
        (Handler,),
        {"slot": slot},
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    print("slot %s build %s serving on %d" % (args.slot, profile["build"], args.port), flush=True)
    thread.join()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="object journal slot")
    parser.add_argument("--slot", required=True)
    parser.add_argument("--generation", type=int, required=True)
    parser.add_argument("--run", default="local")
    parser.add_argument("--endpoint", default=os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566"))
    parser.add_argument("--region", default=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--table", required=True)
    parser.add_argument("--port", type=int, required=True)
    return parser.parse_args(argv)
