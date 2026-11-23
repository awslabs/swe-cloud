"""Recovery pass over the transaction table.

Records are handled the way an operator would by hand: a pending record
nobody has worked on recently is rolled back; a record that already
reached a terminal state is driven the rest of the way; a completed
record is kept for retention_ms and then deleted. Each pass also looks
at what the record table can no longer account for. Nothing here keeps
state between passes — each pass works from what the tables say.
"""

from db import now_ms
from protocol import COMMITTED, PENDING
from seam import seam


class Sweeper(object):
    def __init__(self, proto, rollback_after_ms, retention_ms):
        self.proto = proto
        self.rollback_after_ms = rollback_after_ms
        self.retention_ms = retention_ms

    def sweep_tx(self, txid):
        tx = self.proto.read_tx(txid)
        if tx is None:
            return {"txid": txid, "result": "ABSENT"}
        age = now_ms() - int(tx["last_update_ms"])
        if tx.get("finalized"):
            if age > self.retention_ms:
                self.proto.s.delete(
                    self.proto.s.tx_table,
                    {"txid": txid},
                    clauses=[("eq", "finalized", "1")],
                )
                return {"txid": txid, "result": "DELETED"}
            return {"txid": txid, "result": "FINALIZED"}
        if tx["state"] == PENDING:
            if age > self.rollback_after_ms:
                seam("sweep_candidate", txid=txid, state=tx["state"],
                     version=int(tx["version"]), age_ms=age)
                return {"txid": txid, "result": self.proto.rollback(tx)}
            return {"txid": txid, "result": PENDING}
        # decided but not finished: complete it the way it decided
        if tx["state"] == COMMITTED:
            self.proto.commit_cleanup(tx)
        else:
            self.proto.rollback_cleanup(tx)
        return {"txid": txid, "result": tx["state"]}

    def sweep_all(self):
        results = []
        for item in self.proto.s.scan(self.proto.s.tx_table):
            try:
                results.append(self.sweep_tx(item["txid"]))
            except Exception as err:  # one bad record must not stop the pass
                results.append({"txid": item["txid"], "result": "ERROR", "error": str(err)})
        try:
            results.extend(self.reclaim_stranded())
        except Exception as err:  # reclamation must not stop the pass either
            results.append({"result": "ERROR", "error": str(err)})
        return results

    def reclaim_stranded(self):
        """Reclaim what the record table can no longer account for.

        A coordinator parked in the middle of a request can finish a write
        after recovery has already completed that transaction and reaped
        its record, so a lock marker or a saved image can exist that no
        record explains. The records are the only authority for who still
        exists: whatever none of them explains is leftovers, and the
        participant goes back to being an ordinary writable item.
        """
        live = set(row["txid"] for row in self.proto.s.scan(self.proto.s.tx_table))
        reclaimed = []
        holders = {}
        for row in self.proto.s.scan(self.proto.s.item_table):
            owner = row.get("owner")
            if owner is not None:
                holders.setdefault(owner, set()).add(row["item_id"])
        for owner, item_ids in holders.items():
            if owner in live:
                continue
            for item_id in item_ids:
                self.proto.s.update(
                    self.proto.s.item_table,
                    {"item_id": item_id},
                    removes=["owner", "applied", "locked_at_ms"],
                    clauses=[("eq", "owner", owner)],
                )
                reclaimed.append({"item": item_id, "owner": owner,
                                  "result": "RECLAIMED"})
        for row in self.proto.s.scan(self.proto.s.image_table):
            txid = row["img"].rsplit("#", 1)[0]
            if txid in live:
                continue
            self.proto.s.delete(self.proto.s.image_table, {"img": row["img"]})
            reclaimed.append({"img": row["img"], "result": "RECLAIMED"})
        return reclaimed
