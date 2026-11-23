#!/usr/bin/env python3
"""Retention compactor for the chunk store.

One placement of the service serves one run: it loads the run request,
rewrites the table's index snapshot without the retention-expired chunk
references, records the pending deletion set for the batch, publishes the
new snapshot as the authoritative one, and afterwards sweeps the chunk
objects once the run's deletion grace has passed.

Run:
  python3 worker.py --endpoint http://localhost:4566 --region us-east-1 \
      --bucket <bucket> --table <table> --run-id <run> \
      --worker-id <id> --epoch <n> --work-dir <dir>
"""
import argparse
import decimal
import glob
import json
import os
import sys
import time

import store
from seam import seam

MARKERS_DIR = "markers"


def f(value):
    return float(value) if isinstance(value, decimal.Decimal) else value


class Compactor:
    def __init__(self, args):
        self.args = args
        self.store = store.Store(args.endpoint, args.region, args.bucket,
                                 args.table, args.run_id)
        self.req = self.store.item("META", consistent=True)
        if not self.req:
            raise SystemExit("no run request found for run %s" % args.run_id)
        self.delay = f(self.req["delete_delay_s"])
        self.poll = f(self.req.get("poll_s", 0.1))
        self.markers_dir = os.path.join(args.work_dir, MARKERS_DIR)
        os.makedirs(self.markers_dir, exist_ok=True)

    # -- lifecycle -----------------------------------------------------
    def run(self):
        while True:
            try:
                self.step()
            except Exception as exc:            # keep the placement alive
                sys.stderr.write("step error: %r\n" % (exc,))
                sys.stderr.flush()
            time.sleep(self.poll)

    def step(self):
        head = self.store.item("HEAD", consistent=True)
        batch = self.store.item(self.batch_sk())
        status = (batch or {}).get("status")
        if head["version"] == self.req["out_version"]:
            self.sweep_pending()
        elif status in (None, "NEW", "PREPARED"):
            self.prepare_and_publish(head, batch)
        # DONE or anything else: nothing to do on this tick

    # -- phase one: mark, then publish ----------------------------------
    def prepare_and_publish(self, head, batch):
        index = self.store.get_json(head["index_key"])
        kept, expired = [], []
        for entry in index["chunks"]:
            if f(entry["expires_at"]) <= f(self.req["cutoff"]):
                expired.append(entry["key"])
            else:
                kept.append(entry)
        out_version = self.req["out_version"]
        out_key = self.index_key(out_version)
        self.store.put_json(out_key, {"version": out_version, "chunks": kept})

        marker = self._write_marker(expired, head["version"], out_version)
        if batch is None:
            self.store.put_item(
                {"sk": self.batch_sk(), "status": "PREPARED",
                 "batch": self.req["batch"], "marker": marker["path"],
                 "before_version": head["version"],
                 "out_version": out_version, "created_at": time.time()},
                condition="attribute_not_exists(sk)",
            )
        seam("prepared", worker=self.args.worker_id, epoch=int(self.args.epoch),
             batch=self.req["batch"], keys=sorted(expired))
        self.publish(head["version"], out_version)

    def publish(self, before_version, out_version):
        try:
            self.store.transact_write([
                {"ConditionCheck": {
                    "Key": {"pk": self.store.pk, "sk": "OWNER"},
                    "ConditionExpression": "#e = :epoch",
                    "ExpressionAttributeNames": {"#e": "epoch"},
                    "ExpressionAttributeValues": {
                        ":epoch": int(self.args.epoch)},
                }},
                {"Update": {
                    "Key": {"pk": self.store.pk, "sk": "HEAD"},
                    "ConditionExpression": "#v = :before",
                    "UpdateExpression": "SET #v = :out, #k = :key",
                    "ExpressionAttributeNames": {"#v": "version",
                                                 "#k": "index_key"},
                    "ExpressionAttributeValues": {
                        ":before": before_version, ":out": out_version,
                        ":key": self.index_key(out_version)},
                }},
                {"Update": {
                    "Key": {"pk": self.store.pk, "sk": self.batch_sk()},
                    "ConditionExpression": "#s = :prep",
                    "UpdateExpression": "SET #s = :pub",
                    "ExpressionAttributeNames": {"#s": "status"},
                    "ExpressionAttributeValues": {":prep": "PREPARED",
                                                  ":pub": "PUBLISHED"},
                }},
            ])
        except Exception as exc:
            sys.stderr.write("publish not accepted: %r\n" % (exc,))
            sys.stderr.flush()
            return
        seam("published", worker=self.args.worker_id, epoch=int(self.args.epoch),
             batch=self.req["batch"], version=out_version)

    # -- phase two: sweep -----------------------------------------------
    def sweep_pending(self):
        for marker in self._load_markers():
            if not self._eligible(marker):
                continue
            self._sweep_marker(marker)

    def _sweep_marker(self, marker):
        for key in list(marker["keys"]):
            self.store.s3.delete_object(Bucket=self.store.bucket, Key=key)
            seam("object_deleted", worker=self.args.worker_id,
                 epoch=int(self.args.epoch), batch=self.req["batch"], key=key)
            marker["keys"].remove(key)
            self._flush_marker(marker)
        if self._mark_done(marker):
            self._remove_marker(marker)

    def _eligible(self, marker):
        return time.time() >= f(marker["created_at"]) + self.delay

    def _mark_done(self, marker):
        try:
            self.store.update_item(
                self.batch_sk(),
                "#s = :pub",
                "SET #s = :done",
                {":pub": "PUBLISHED", ":done": "DONE"},
                names={"#s": "status"},
            )
            return True
        except Exception as exc:
            sys.stderr.write("completion not accepted: %r\n" % (exc,))
            sys.stderr.flush()
            return False

    # -- marker records -------------------------------------------------
    def _write_marker(self, keys, before_version, out_version):
        path = os.path.join(self.markers_dir,
                            "%d.json" % int(time.time() * 1000))
        marker = {"batch": self.req["batch"], "keys": list(keys),
                  "created_at": time.time(),
                  "before_version": before_version,
                  "out_version": out_version, "path": path}
        with open(path, "w") as fh:
            json.dump(marker, fh)
        return marker

    def _load_markers(self):
        markers = []
        for path in sorted(glob.glob(os.path.join(self.markers_dir,
                                                  "*.json"))):
            try:
                with open(path) as fh:
                    marker = json.load(fh)
                marker["path"] = path
                markers.append(marker)
            except (OSError, ValueError):
                continue
        return markers

    def _flush_marker(self, marker):
        with open(marker["path"], "w") as fh:
            json.dump({k: v for k, v in marker.items() if k != "path"}, fh)

    def _remove_marker(self, marker):
        try:
            os.remove(marker["path"])
        except OSError:
            pass

    # -- helpers ---------------------------------------------------------
    def batch_sk(self):
        return "BATCH#%s" % self.req["batch"]

    def index_key(self, version):
        return "%s/%s/%s/%s.json" % (self.req["index_prefix"],
                                     self.req["tenant"],
                                     self.req["table_id"], version)


def main():
    ap = argparse.ArgumentParser(description="chunk store retention compactor")
    ap.add_argument("--endpoint", required=True)
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--table", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--worker-id", required=True)
    ap.add_argument("--epoch", required=True, type=int)
    ap.add_argument("--work-dir", required=True)
    args = ap.parse_args()

    compactor = Compactor(args)
    seam("ready", worker=args.worker_id, epoch=args.epoch, run=args.run_id)
    compactor.run()


if __name__ == "__main__":
    main()
