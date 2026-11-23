"""Durable multi-item transaction protocol.

A transaction is recorded in the tx table before any participant is
touched: its state, a version number that increases by one for every
request appended, the request list itself, and the last time the record
was worked on. Every participant item carries an owner attribute while
it belongs to a transaction, plus an applied marker once the request's
write has been performed. The previous value of each participant is
copied to the image table before its request is applied, so a
transaction that has not committed can be undone.

Every decision and every cleanup step is a write against these three
tables, so any process can pick up another process's work.

Nothing any process remembers is authority. A snapshot of a record, or
of an item image, only says what was true when it was read: the record
itself decides what a terminal write is allowed to do, and item
ownership decides what cleanup is allowed to overwrite. A snapshot of a
record does not even guarantee the record still exists -- completed
records are reaped at the retention time, so a process that was parked
mid-request can wake up holding nothing but its own intentions.
"""

import json

from db import now_ms
from seam import seam

PENDING = "PENDING"
COMMITTED = "COMMITTED"
ROLLED_BACK = "ROLLED_BACK"

RETRY_BUDGET = 20


class ProtocolError(RuntimeError):
    pass


class CompletedTransaction(ProtocolError):
    def __init__(self, state):
        super(CompletedTransaction, self).__init__("transaction already " + state)
        self.state = state


class ItemConflict(ProtocolError):
    def __init__(self, item_id, owner):
        super(ItemConflict, self).__init__(
            "item %s is owned by another transaction" % item_id)
        self.item_id = item_id
        self.owner = owner


class Protocol(object):
    def __init__(self, store):
        self.s = store

    # ---- reading records

    @staticmethod
    def image_key(txid, rid):
        return "%s#%d" % (txid, rid)

    def read_tx(self, txid):
        item = self.s.get(self.s.tx_table, {"txid": txid})
        if item is None:
            return None
        item["requests"] = json.loads(item.get("requests") or "[]")
        return item

    def read_item(self, item_id):
        return self.s.get(self.s.item_table, {"item_id": item_id})

    def read_image(self, txid, rid):
        return self.s.get(self.s.image_table, {"img": self.image_key(txid, rid)})

    # ---- coordinator side

    def begin(self, txid):
        ok = self.s.put(
            self.s.tx_table,
            {
                "txid": txid,
                "state": PENDING,
                "version": 0,
                "requests": "[]",
                "last_update_ms": now_ms(),
            },
            clauses=[("not_exists", "txid")],
        )
        if not ok:
            raise ProtocolError("transaction id %s already in use" % txid)

    def append(self, txid, item_id, value):
        for _ in range(RETRY_BUDGET + 1):
            tx = self.read_tx(txid)
            if tx is None:
                raise ProtocolError("unknown transaction %s" % txid)
            if tx["state"] != PENDING:
                raise CompletedTransaction(tx["state"])
            rid = int(tx["version"]) + 1
            requests = list(tx["requests"]) + [{"rid": rid, "item": item_id, "val": value}]
            ok = self.s.update(
                self.s.tx_table,
                {"txid": txid},
                sets={
                    "requests": json.dumps(requests),
                    "version": rid,
                    "last_update_ms": now_ms(),
                },
                clauses=[("eq", "state", PENDING), ("eq", "version", int(tx["version"]))],
            )
            if ok:
                seam("after_append", txid=txid, rid=rid, version=rid, item=item_id)
                return rid
        raise ProtocolError("could not append to %s under contention" % txid)

    def apply(self, txid, item_id):
        tx = self.read_tx(txid)
        if tx is None:
            raise ProtocolError("unknown transaction %s" % txid)
        request = None
        for candidate in tx["requests"]:
            if candidate["item"] == item_id:
                request = candidate
        if request is None:
            raise ProtocolError("no request in %s for item %s" % (txid, item_id))
        self.drive_request(tx, request)
        return request["rid"]

    def drive_request(self, tx, request):
        # 1. take the item's lock
        ok = self.s.update(
            self.s.item_table,
            {"item_id": request["item"]},
            sets={"owner": tx["txid"], "locked_at_ms": now_ms()},
            clauses=[("not_exists", "owner")],
        )
        if not ok:
            current = self.read_item(request["item"])
            if current is None or current.get("owner") != tx["txid"]:
                raise ItemConflict(request["item"], (current or {}).get("owner"))
        seam("after_lock", txid=tx["txid"], rid=request["rid"], item=request["item"])
        # 2. save the previous value, once, before the write
        current = self.read_item(request["item"])
        if "applied" not in current:
            self.s.put(
                self.s.image_table,
                {
                    "img": self.image_key(tx["txid"], request["rid"]),
                    "item_id": request["item"],
                    "val": current.get("val"),
                },
                clauses=[("not_exists", "img")],
            )
        # 3. the record may have been decided while we worked on the item
        tx = self.read_tx(tx["txid"])
        if tx["state"] == COMMITTED:
            self.commit_cleanup(tx)
            raise CompletedTransaction(COMMITTED)
        if tx["state"] == ROLLED_BACK:
            self.rollback_cleanup(tx)
            raise CompletedTransaction(ROLLED_BACK)
        # 4. perform the write, at most once
        self.s.update(
            self.s.item_table,
            {"item_id": request["item"]},
            sets={"val": request["val"], "applied": "1"},
            clauses=[("eq", "owner", tx["txid"]), ("not_exists", "applied")],
        )
        seam("after_apply", txid=tx["txid"], rid=request["rid"],
             item=request["item"], val=request["val"])

    def verify_and_catch_up(self, tx):
        """Drive every request that is not yet fully applied."""
        for request in tx["requests"]:
            current = self.read_item(request["item"])
            if (current is not None and current.get("owner") == tx["txid"]
                    and "applied" in current):
                continue
            self.drive_request(tx, request)

    def commit(self, txid):
        for _ in range(RETRY_BUDGET + 1):
            tx = self.read_tx(txid)
            if tx is None:
                return {"txid": txid, "result": "UNKNOWN"}
            if tx.get("finalized"):
                return {"txid": txid, "result": tx["state"]}
            if tx["state"] == COMMITTED:
                self.commit_cleanup(tx)
                return {"txid": txid, "result": COMMITTED}
            if tx["state"] == ROLLED_BACK:
                self.rollback_cleanup(tx)
                return {"txid": txid, "result": ROLLED_BACK}
            version = int(tx["version"])
            self.verify_and_catch_up(tx)
            seam("before_decide", txid=txid, version=version, state=PENDING)
            ok = self.s.update(
                self.s.tx_table,
                {"txid": txid},
                sets={"state": COMMITTED, "last_update_ms": now_ms()},
                clauses=[
                    ("eq", "state", PENDING),
                    ("eq", "version", version),
                    ("not_exists", "finalized"),
                ],
            )
            seam("after_decide_attempt", txid=txid, attempted=COMMITTED, succeeded=ok)
            if ok:
                seam("after_decide", txid=txid, state=COMMITTED)
                self.commit_cleanup(tx)
                return {"txid": txid, "result": COMMITTED}
            # the decision did not land: the record changed under us, retry
        return {"txid": txid, "result": "CONTENDED"}

    def rollback(self, tx):
        """Decide ROLLED_BACK for a record that was pending at the version
        the caller verified, then finish the record the way it is decided.

        The attempt is only valid if the record is still exactly what the
        caller read. If the service says otherwise, the current record
        decides what happens next: a decision that already landed is
        carried to completion, never overwritten, and a still-pending
        record with a larger request set is retried as it now stands.
        """
        for _ in range(RETRY_BUDGET + 1):
            ok = self.s.update(
                self.s.tx_table,
                {"txid": tx["txid"]},
                sets={"state": ROLLED_BACK, "last_update_ms": now_ms()},
                clauses=[
                    ("eq", "state", PENDING),
                    ("eq", "version", int(tx["version"])),
                    ("not_exists", "finalized"),
                ],
            )
            if ok:
                decided = dict(tx)
                decided["state"] = ROLLED_BACK
                self.rollback_cleanup(decided)
                return ROLLED_BACK
            fresh = self.read_tx(tx["txid"])
            if fresh is None:
                return "UNKNOWN"
            if fresh.get("finalized") or fresh["state"] != PENDING:
                # someone else's decision stands; finish it their way
                if fresh["state"] == COMMITTED:
                    self.commit_cleanup(fresh)
                    return COMMITTED
                self.rollback_cleanup(fresh)
                return fresh["state"]
            # still pending, but a different request set than we read:
            # re-run the attempt against what the record now says
            tx = fresh
        return "CONTENDED"

    # ---- cleanup

    def commit_cleanup(self, tx):
        """After a commit: keep the new values, release every item, drop
        the saved copies, and mark the record complete."""
        for request in tx["requests"]:
            self.s.update(
                self.s.item_table,
                {"item_id": request["item"]},
                removes=["owner", "applied", "locked_at_ms"],
                clauses=[("eq", "owner", tx["txid"])],
            )
        for request in tx["requests"]:
            self.s.delete(
                self.s.image_table,
                {"img": self.image_key(tx["txid"], request["rid"])},
            )
        self.finalize(tx, COMMITTED)

    def rollback_cleanup(self, tx):
        """After a rollback: put every item back and mark the record
        complete. Each item is restored before its saved copy is dropped,
        and any step another pass already finished is skipped, so a pass
        that was interrupted partway through can simply be run again."""
        for request in tx["requests"]:
            self.restore_item(tx, request)
        self.finalize(tx, ROLLED_BACK)

    def restore_item(self, tx, request):
        image = self.read_image(tx["txid"], request["rid"])
        if image is not None:
            seam("before_restore", txid=tx["txid"], rid=request["rid"],
                 item=request["item"], image_val=image.get("val"))
            # the restore only lands while this transaction still owns the
            # item; if it does not, the item has already moved past this
            # transaction and must be left exactly as it is
            restored = self.s.put(
                self.s.item_table,
                {"item_id": request["item"], "val": image.get("val")},
                clauses=[("eq", "owner", tx["txid"])],
            )
            seam("after_restore", txid=tx["txid"], rid=request["rid"],
                 item=request["item"], restored=restored)
            self.s.delete(
                self.s.image_table,
                {"img": self.image_key(tx["txid"], request["rid"])},
            )
            return
        # no saved copy: either this request never got as far as saving one,
        # or another pass already restored it and consumed the copy. What
        # the item says now decides which.
        current = self.read_item(request["item"])
        if current is not None and current.get("owner") == tx["txid"]:
            if "applied" in current:
                raise ProtocolError(
                    "item %s was applied but has no saved image" % request["item"])
            self.s.update(
                self.s.item_table,
                {"item_id": request["item"]},
                removes=["owner", "locked_at_ms"],
                clauses=[("eq", "owner", tx["txid"]), ("not_exists", "applied")],
            )

    def finalize(self, tx, state):
        ok = self.s.update(
            self.s.tx_table,
            {"txid": tx["txid"]},
            sets={"finalized": "1", "last_update_ms": now_ms()},
            clauses=[("eq", "state", state), ("not_exists", "finalized")],
        )
        if not ok:
            fresh = self.read_tx(tx["txid"])
            if fresh is None or not fresh.get("finalized"):
                raise ProtocolError("could not finalize %s" % tx["txid"])
