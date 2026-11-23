"""A single worker thread for the visibility extender.

Like a one-thread ``ScheduledThreadPoolExecutor``: one fixed-rate task plus one-shot tasks, never
two of them at the same time. A fixed-rate run that takes longer than its period is not skipped: the
next run starts as soon as the late one is done, and the schedule keeps its original phase
(start + k * period).
"""

import collections
import logging
import threading
import time

LOG = logging.getLogger(__name__)


class SingleThreadScheduler:

    def __init__(self, name):
        self._cond = threading.Condition()
        self._tasks = collections.deque()
        self._periodic = None
        self._shutdown = False
        self._thread = threading.Thread(target=self._loop, name=name, daemon=True)
        self._thread.start()

    def schedule_at_fixed_rate(self, task, initial_delay, period):
        with self._cond:
            self._periodic = [task, time.monotonic() + initial_delay, period]
            self._cond.notify()

    def execute(self, task):
        """Runs ``task`` once on the worker thread, after the run in progress (if any)."""
        with self._cond:
            if self._shutdown:
                raise RuntimeError("scheduler is shut down")
            self._tasks.append(task)
            self._cond.notify()

    def shutdown(self, wait=True, timeout=None):
        with self._cond:
            self._shutdown = True
            self._tasks.clear()
            self._cond.notify()
        if wait and threading.current_thread() is not self._thread:
            self._thread.join(timeout)

    def _next_task(self):
        with self._cond:
            while True:
                if self._shutdown:
                    return None
                if self._tasks:
                    return self._tasks.popleft()
                if self._periodic is None:
                    self._cond.wait()
                    continue
                task, due, period = self._periodic
                delay = due - time.monotonic()
                if delay <= 0:
                    self._periodic[1] = due + period
                    return task
                self._cond.wait(delay)

    def _loop(self):
        while True:
            task = self._next_task()
            if task is None:
                return
            try:
                task()
            except Exception:  # a failing run must not kill the schedule
                LOG.exception("scheduled task failed")
