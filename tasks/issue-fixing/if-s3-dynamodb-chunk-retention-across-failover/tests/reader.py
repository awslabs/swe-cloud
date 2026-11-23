#!/usr/bin/env python3
"""Query-frontend simulator used by the verification harness.

Resolves HEAD, serves lookups from a pinned snapshot, verifies chunk
digests, and writes what it served to the run's AUDIT items. One reader
runs with --mode stale (keeps serving the snapshot it pinned for a
bounded time after the pointer moves on, then runs one last spread-out
query before refreshing — and treats the snapshot it refreshes to the
same way) and one with --mode follow (re-pins as soon as the pointer
moves).
"""
import argparse
import hashlib
import json
import socket
import threading
import time

import boto3
from botocore.config import Config
from decimal import Decimal
from botocore.exceptions import ClientError

CFG = Config(retries={"max_attempts": 4, "mode": "standard"},
             connect_timeout=5, read_timeout=10)


class Frontend:
    def __init__(self, args):
        self.args = args
        self.s3 = boto3.client("s3", endpoint_url=args.endpoint,
                               region_name=args.region, config=CFG)
        self.ddb = boto3.resource("dynamodb", endpoint_url=args.endpoint,
                                  region_name=args.region, config=CFG)
        self.table = self.ddb.Table(args.table)
        self.pk = "RUN#%s" % args.run
        self.seq = 0
        self.stats = {"cycles": 0, "missing": 0, "corrupt": 0, "errors": 0,
                      "first_ts": None, "last_ts": None,
                      "last_old_ts": None, "first_new_ts": None}
        self.lock = threading.Lock()
        self.rxbuf = b""

    # -- control ---------------------------------------------------------
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(90.0)
        self.sock.connect(self.args.socket)
        self.send({"ev": "hello", "reader": self.args.reader})

    def send(self, obj):
        self.sock.sendall((json.dumps(obj) + "\n").encode())

    def poll_op(self):
        if b"\n" in self.rxbuf:
            line, self.rxbuf = self.rxbuf.split(b"\n", 1)
            return json.loads(line.decode())
        self.sock.settimeout(0.005)
        try:
            data = self.sock.recv(65536)
        except (socket.timeout, BlockingIOError):
            return None
        except (ConnectionResetError, OSError):
            return {"op": "stop"}
        finally:
            self.sock.settimeout(90.0)
        if not data:
            return {"op": "stop"}
        self.rxbuf += data
        if b"\n" in self.rxbuf:
            line, self.rxbuf = self.rxbuf.split(b"\n", 1)
            return json.loads(line.decode())
        return None

    # -- snapshot serving --------------------------------------------------
    def resolve_head(self):
        return self.table.get_item(Key={"pk": self.pk, "sk": self.args.head_sk},
                                   ConsistentRead=True).get("Item")

    def load_snapshot(self, head):
        resp = self.s3.get_object(Bucket=self.args.bucket,
                                  Key=head["index_key"])
        return json.loads(resp["Body"].read())

    def query(self, snapshot, spread=0.0):
        """Fetch every chunk the snapshot references; verify digests."""
        started = time.time()
        if spread:
            time.sleep(spread)
        missing, corrupt = [], []
        entries = snapshot["chunks"]
        for i, entry in enumerate(entries):
            if spread and i:
                time.sleep(spread / max(1, len(entries)))
            try:
                resp = self.s3.get_object(Bucket=self.args.bucket,
                                          Key=entry["key"])
                body = resp["Body"].read()
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code")
                if code in ("NoSuchKey", "404"):
                    missing.append(entry["key"])
                    continue
                raise
            if hashlib.sha256(body).hexdigest() != entry["digest"]:
                corrupt.append(entry["key"])
        return {"t0": started, "t1": time.time(),
                "version": snapshot["version"], "n": len(entries),
                "missing": missing, "corrupt": corrupt, "errors": 0}

    def audit(self, kind, payload):
        self.seq += 1
        item = {"pk": self.pk,
                "sk": "AUDIT#%s#%06d" % (self.args.reader, self.seq),
                "reader": self.args.reader, "kind": kind}
        item.update({k: (Decimal(str(v)) if isinstance(v, float) else v)
                     for k, v in payload.items()})
        self.table.put_item(Item=item)

    def note(self, cycle):
        with self.lock:
            st = self.stats
            st["cycles"] += 1
            st["missing"] += len(cycle["missing"])
            st["corrupt"] += len(cycle["corrupt"])
            st["errors"] += cycle.get("errors", 0)
            st["first_ts"] = st["first_ts"] or cycle["t0"]
            st["last_ts"] = cycle["t1"]
            if cycle["version"] == self.in_version:
                st["last_old_ts"] = cycle["t1"]
            elif st["first_new_ts"] is None:
                st["first_new_ts"] = cycle["t1"]

    # -- main loop ---------------------------------------------------------
    def run(self):
        head = self.resolve_head()
        self.in_version = head["version"]
        self.snapshot = self.load_snapshot(head)
        self.send({"ev": "pinned", "version": head["version"]})

        refresh_at = None
        heart_t0, heart_cycles, heart_problems, heart_errors = \
            time.time(), 0, 0, 0
        while True:
            op = self.poll_op()
            if op and op.get("op") == "stop":
                break

            try:
                cycle = self.query(self.snapshot)
            except Exception:
                cycle = {"t0": time.time(), "t1": time.time(),
                         "version": self.snapshot.get("version", "?"),
                         "n": 0, "missing": [], "corrupt": [], "errors": 1}
            self.note(cycle)
            heart_cycles += 1
            heart_problems += len(cycle["missing"]) + len(cycle["corrupt"])
            heart_errors += cycle.get("errors", 0)
            if cycle["missing"] or cycle["corrupt"]:
                self.audit("query", cycle)

            if self.args.mode == "stale" and refresh_at is None:
                head = self.resolve_head()
                if head["version"] != self.snapshot.get("version"):
                    refresh_at = time.time() + self.args.refresh_after_flip
            elif self.args.mode == "stale" and refresh_at is not None \
                    and time.time() >= refresh_at:
                # one last spread-out query against the old snapshot,
                # then refresh: the bounded straggler the grace must cover
                try:
                    cycle = self.query(self.snapshot,
                                       spread=self.args.flight * 0.5)
                except Exception:
                    cycle = {"t0": time.time(), "t1": time.time(),
                             "version": self.snapshot.get("version", "?"),
                             "n": 0, "missing": [], "corrupt": [],
                             "errors": 1}
                self.note(cycle)
                heart_cycles += 1
                heart_problems += len(cycle["missing"]) + len(cycle["corrupt"])
                heart_errors += cycle.get("errors", 0)
                if cycle["missing"] or cycle["corrupt"]:
                    self.audit("query", cycle)
                self.snapshot = self.load_snapshot(self.resolve_head())
                # the frontend serves the snapshot it refreshed to like any
                # other: a further turnover opens another bounded window
                refresh_at = None
            elif self.args.mode == "follow":
                head = self.resolve_head()
                if head["version"] != self.snapshot.get("version"):
                    self.snapshot = self.load_snapshot(head)

            now = time.time()
            if now - heart_t0 >= 1.0:
                self.audit("heart", {
                    "t0": heart_t0, "t1": now, "cycles": heart_cycles,
                    "problems": heart_problems, "errors": heart_errors,
                    "served": cycle["version"],
                    "old_pinned": refresh_at is not None})
                heart_t0, heart_cycles = now, 0
                heart_problems, heart_errors = 0, 0
            time.sleep(self.args.poll)

        with self.lock:
            summary = dict(self.stats)
        summary.update({"in_version": self.in_version,
                        "last_version": self.snapshot.get("version")})
        self.audit("summary", summary)
        self.send({"ev": "done", **summary})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", required=True)
    ap.add_argument("--endpoint", required=True)
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--table", required=True)
    ap.add_argument("--run", required=True)
    ap.add_argument("--reader", required=True)
    ap.add_argument("--mode", choices=("stale", "follow"), required=True)
    ap.add_argument("--head-sk", default="HEAD")
    ap.add_argument("--refresh-after-flip", type=float, default=6.0)
    ap.add_argument("--flight", type=float, default=1.0)
    ap.add_argument("--poll", type=float, default=0.1)
    args = ap.parse_args()
    fe = Frontend(args)
    fe.connect()
    fe.run()


if __name__ == "__main__":
    main()
