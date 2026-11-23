"""How far the relay may move on each poll.

Positions come from a sequence, so they are handed out before the writing transaction commits:
rows become visible in commit order, not position order, and a missing position may still be
filled by a transaction that is in progress. Positions also go missing for good (a writer rolls
back, a duplicate insert is dropped by ON CONFLICT).

A missing position q can be passed once two things are known:

* q was drawn before some observation O: a committed row above q was visible in O's snapshot,
  and the sequence (CACHE 1) hands out values in time order;
* every transaction that could have drawn q has finished: producers take their xid before they
  insert (outbox/writer.py), so q's writer had an xid before O. An xid X taken *after* O is larger
  than all of them, and a later snapshot whose xmin is above X has none of them in progress.

So on seeing a hole the relay records X = pg_current_xact_id() and the highest visible position
C. Once a poll's snapshot has xmin > X, every position <= C still missing in that same snapshot is
dead. Nothing here depends on elapsed time, and nothing needs to survive a restart: a fresh
process simply observes again, which is proven as soon as the transactions open at that moment
have ended.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .store import OutboxRow


@dataclass(frozen=True)
class Plan:
    rows: list[OutboxRow]   # rows to send now, in order
    frontier: int           # checkpoint position once they are sent


class FrontierTracker:
    def __init__(self, take_xid: Callable[[], int]):
        self.take_xid = take_xid
        self._dead_through = 0                          # missing positions <= this never commit
        self._pending: tuple[int, int] | None = None    # (xid bound, highest visible position)

    def plan(self, start: int, xmin: int, rows: list[OutboxRow]) -> Plan:
        """`rows` are the rows above `start`, ascending, read in the same snapshot as `xmin`."""
        if self._pending is not None and xmin > self._pending[0]:
            self._dead_through = max(self._dead_through, self._pending[1])
            self._pending = None

        ready: list[OutboxRow] = []
        expected = start + 1
        for row in rows:
            if row.position != expected and row.position - 1 > self._dead_through:
                break
            ready.append(row)
            expected = row.position + 1

        if len(ready) < len(rows) and self._pending is None:
            # taken after the snapshot that showed the hole, so it is above every possible writer
            self._pending = (self.take_xid(), rows[-1].position)
        return Plan(ready, ready[-1].position if ready else start)
