"""How far the relay may move on each poll.

Positions come from a sequence, so they are handed out before the writing transaction commits:
rows become visible in commit order, not position order, and a missing position below visible
rows may still be filled. The relay therefore only sends a contiguous run of positions and holds
at the first missing one. Positions also go missing for good (a writer rolls back, a duplicate
insert is dropped), so a position that has held the relay for `stale_gap_seconds` is stepped
over.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .store import OutboxRow

log = logging.getLogger("relay.frontier")


@dataclass(frozen=True)
class Plan:
    rows: list[OutboxRow]   # rows to send now, in order
    frontier: int           # checkpoint position once they are sent


def _contiguous(rows: list[OutboxRow]) -> list[OutboxRow]:
    run: list[OutboxRow] = []
    for row in rows:
        if run and row.position != run[-1].position + 1:
            break
        run.append(row)
    return run


class FrontierTracker:
    def __init__(self, stale_gap_seconds: float, clock=time.monotonic):
        self.stale_gap_seconds = stale_gap_seconds
        self.clock = clock
        self._held_at: int | None = None
        self._held_since = 0.0

    def plan(self, start: int, rows: list[OutboxRow]) -> Plan:
        """`rows` are the outbox rows above `start`, ascending, as read in one statement."""
        ready = _contiguous(rows)
        frontier = ready[-1].position if ready else start
        if len(ready) == len(rows):
            self._held_at = None
            return Plan(ready, frontier)

        now = self.clock()
        if self._held_at != frontier:
            self._held_at, self._held_since = frontier, now
            return Plan(ready, frontier)
        if now - self._held_since < self.stale_gap_seconds:
            return Plan(ready, frontier)

        rest = _contiguous(rows[len(ready):])
        log.warning("position %d still missing after %.1fs; stepping over it to %d",
                    frontier + 1, now - self._held_since, rest[-1].position)
        self._held_at = None
        return Plan(ready + rest, rest[-1].position)
