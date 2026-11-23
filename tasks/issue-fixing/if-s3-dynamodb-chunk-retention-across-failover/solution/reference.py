#!/usr/bin/env python3
"""Retention compactor for the chunk store.

One placement of the service serves one run: it loads the run request,
rewrites the table's index snapshot without the retention-expired chunk
references, records the pending deletion set for the batch as a deletion
marker in the chunk bucket, publishes the new snapshot as the
authoritative one, and afterwards sweeps the chunk objects once the
run's deletion grace has passed.

Two things are recorded durably, in the bucket and table the run was
given, so that a replacement placement inherits them instead of
re-deriving later ones: the moment the run's own publication became
known to be in effect, and — per chunk — when the stretch in which no
authoritative snapshot of any of the tenant's tables referenced the
chunk began. A chunk's grace runs from whichever of those moments binds
it, because a table other than the one this run compacts can keep
serving a chunk in its own snapshots until it turns over on its own
schedule — and a later snapshot of that table can list a boundary chunk
again, so the recorded moment is written anew at every release a
placement witnesses; only a release nobody witnessed is inherited
as it stands.

Run:
  python3 worker.py --endpoint http://localhost:4566 --region us-east-1 \
      --bucket <bucket> --table <table> --run-id <run> \
      --worker-id <id> --epoch <n> --work-dir <dir>
"""
import argparse
import decimal
import json
import sys
import time

import store
from seam import seam


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
        self.serving = set()   # keys this placement has seen referenced

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
        batch = self.store.item(self.batch_sk(), consistent=True)
        status = (batch or {}).get("status")
        if status == "DONE":
            return
        if head["version"] == self.req["out_version"]:
            self.sweep_published(batch)
        elif status in (None, "NEW", "PREPARED"):
            self.prepare_and_publish(head, batch)

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
        self.store.put_json(self.index_key(out_version),
                            {"version": out_version, "chunks": kept})
        self.store.put_json(self.marker_key(), {
            "batch": self.req["batch"], "keys": sorted(expired),
            "created_at": time.time(),
            "before_version": head["version"], "out_version": out_version,
        })
        if batch is None:
            self.store.put_item(
                {"sk": self.batch_sk(), "status": "PREPARED",
                 "batch": self.req["batch"], "marker_key": self.marker_key(),
                 "before_version": head["version"],
                 "out_version": out_version, "created_at": time.time(),
                 "remaining_keys": sorted(expired), "progress_version": 0,
                 "unreferenced_at": {}},
                condition="attribute_not_exists(sk)",
            )
        seam("prepared", worker=self.args.worker_id,
             epoch=int(self.args.epoch), batch=self.req["batch"],
             keys=sorted(expired))
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
        seam("published", worker=self.args.worker_id,
             epoch=int(self.args.epoch), batch=self.req["batch"],
             version=out_version)
        self.confirm_publication()

    def confirm_publication(self):
        """Record, once and durably, when the new snapshot became known
        to be in effect, and arm the grace from that observation.

        A placement that died between the commit and this record leaves
        nothing behind; its replacement re-observes the committed HEAD and
        arms the grace from its own (necessarily later, therefore still
        conservative) observation. The conditional keeps any later replay
        from moving an already-armed grace.
        """
        head = self.store.item("HEAD", consistent=True)
        if head["version"] != self.req["out_version"]:
            return
        now = time.time()
        try:
            self.store.update_item(
                self.batch_sk(),
                "attribute_not_exists(confirmed_at) AND #s = :pub",
                "SET confirmed_at = :now, eligible_at = :elig",
                {":pub": "PUBLISHED", ":now": now,
                 ":elig": now + self.delay},
                names={"#s": "status"},
            )
        except Exception as exc:
            sys.stderr.write("confirmation not accepted: %r\n" % (exc,))
            sys.stderr.flush()

    # -- phase two: sweep -----------------------------------------------
    def sweep_published(self, batch):
        """One pass per tick.

        A chunk may be deleted only once no authoritative snapshot of any
        table still references it, and only after the grace has passed
        since the last table stopped serving it. A table's later snapshot
        can list a chunk again after an earlier one dropped it, so each
        witnessed release re-records the stretch's beginning durably; a
        replacement inherits that instant instead of arming its own.
        """
        if not (batch or {}).get("eligible_at"):
            self.confirm_publication()
            return
        marker = self.store.get_json(self.marker_key())
        authorized = list(marker["keys"])
        remaining = [k for k in authorized if self.store.head(k)]
        if not remaining:
            self._mark_done()
            return
        heads = self.index_heads()
        for key in remaining:
            if self.referencing_tables(key, heads):
                self.serving.add(key)   # a table still serves this chunk
                continue
            if key in self.serving or \
                    key not in (batch.get("unreferenced_at") or {}):
                self._anchor_unreferenced(key, batch)
                self.serving.discard(key)
            if time.time() < self._earliest(batch, key):
                continue
            self.store.s3.delete_object(Bucket=self.store.bucket, Key=key)
            seam("object_deleted", worker=self.args.worker_id,
                 epoch=int(self.args.epoch), batch=self.req["batch"],
                 key=key)
            self._record_progress(batch, authorized, key)

    def index_heads(self):
        """Every authoritative index pointer the run's table carries:
        this run's own HEAD and one HEAD#<table-id> row per other table
        of the tenant."""
        resp = self.store.table.query(
            KeyConditionExpression="pk = :pk AND begins_with(sk, :h)",
            ExpressionAttributeValues={":pk": self.store.pk, ":h": "HEAD"},
            ConsistentRead=True)
        return [item for item in resp.get("Items", [])
                if item.get("index_key")]

    def referencing_tables(self, key, heads):
        """The tables whose authoritative snapshot still lists the key."""
        out = []
        for item in heads:
            try:
                index = self.store.get_json(item["index_key"])
            except Exception:
                out.append(item)   # unreadable: assume it is referenced
                continue
            if any(entry.get("key") == key for entry in index["chunks"]):
                out.append(item)
        return out

    def _earliest(self, batch, key):
        """Earliest safe deletion for one key: the run's own publication
        anchor, or the anchor of the moment it stopped being referenced
        by the last table to serve it — whichever binds later."""
        anchors = batch.get("unreferenced_at") or {}
        return max(f(batch.get("eligible_at") or 0),
                   f(anchors.get(key, 0)) + self.delay)

    def _anchor_unreferenced(self, key, batch):
        """Record durably when the current unreferenced stretch began.

        The write overwrites any anchor an earlier stretch left behind:
        the grace has to run from the latest release, not the first, so
        it moves forward each time this placement sees a reference go
        away. Only a placement that never saw the chunk referenced keeps
        an inherited anchor untouched instead of restarting the clock.
        """
        when = time.time()
        try:
            self.store.update_item(
                self.batch_sk(),
                "attribute_exists(#u)",
                "SET #u.#k = :now",
                {":now": when},
                names={"#u": "unreferenced_at", "#k": key},
            )
        except Exception as exc:
            sys.stderr.write("anchor not accepted: %r\n" % (exc,))
            sys.stderr.flush()
        fresh = self.store.item(self.batch_sk(), consistent=True) or {}
        got = f((fresh.get("unreferenced_at") or {}).get(key, when))
        batch.setdefault("unreferenced_at", {})[key] = got
        return got

    def _record_progress(self, batch, authorized, key):
        prev = int(batch.get("progress_version", 0))
        remaining = [k for k in (batch.get("remaining_keys") or authorized)
                     if k != key]
        try:
            self.store.update_item(
                self.batch_sk(),
                "#s = :pub AND #p = :prev",
                "SET remaining_keys = :rem, progress_version = :next",
                {":pub": "PUBLISHED", ":prev": prev, ":rem": remaining,
                 ":next": prev + 1},
                names={"#s": "status", "#p": "progress_version"},
            )
            batch["progress_version"] = prev + 1
            batch["remaining_keys"] = remaining
        except Exception as exc:
            sys.stderr.write("progress not accepted: %r\n" % (exc,))
            sys.stderr.flush()

    def _mark_done(self):
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

    # -- helpers ---------------------------------------------------------
    def batch_sk(self):
        return "BATCH#%s" % self.req["batch"]

    def index_key(self, version):
        return "%s/%s/%s/%s.json" % (self.req["index_prefix"],
                                     self.req["tenant"],
                                     self.req["table_id"], version)

    def marker_key(self):
        return "%s/%s.json" % (self.req["marker_prefix"], self.req["batch"])


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
