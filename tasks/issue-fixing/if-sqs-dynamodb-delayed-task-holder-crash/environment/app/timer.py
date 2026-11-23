"""Local schedule of future callbacks.

Entries live in this process only. They are compared against the wall clock
at every pass, so an entry that becomes due while the process is not running
is still dispatched as soon as the process runs again — and an entry is lost
outright if the process goes away.
"""

import heapq
import time


class Schedule:
    """A heap of (due_at, seq, callback, delivery) entries."""

    def __init__(self, clock=time.time):
        self._queue = []
        self._seq = 0
        self._clock = clock

    def call_at(self, due_at, callback, delivery):
        self._seq += 1
        heapq.heappush(self._queue, (float(due_at), self._seq, callback, delivery))

    def cancel(self, delivery):
        marker = id(delivery)
        self._queue = [e for e in self._queue if id(e[3]) != marker]
        heapq.heapify(self._queue)

    def due(self):
        """Return and remove every entry whose time has passed."""
        now = self._clock()
        ready = []
        while self._queue and self._queue[0][0] <= now:
            entry = heapq.heappop(self._queue)
            ready.append((entry[2], entry[3]))
        return ready

    def pending(self):
        return len(self._queue)

    def next_wake(self):
        if not self._queue:
            return None
        return self._queue[0][0]
