"""In-memory interruption event store (per pod), ported from pkg/interruptioneventstore."""

import logging
import threading
from datetime import datetime, timedelta, timezone

log = logging.getLogger("nth.store")


class Store:
    def __init__(self, config):
        self.config = config
        self._lock = threading.RLock()
        self._events = {}
        self._ignored = set()
        self.workers = threading.BoundedSemaphore(config.workers)
        self._calls_since_last_clean = 0
        self._cleaning_period = 7200

    def add_interruption_event(self, event):
        with self._lock:
            if event.event_id in self._events:
                return
            log.info("adding new event to the event store: %s (%s, node %s)",
                     event.event_id, event.kind, event.node_name)
            self._events[event.event_id] = event

    def cancel_interruption_event(self, event_id):
        with self._lock:
            self._events.pop(event_id, None)

    def ignore_event(self, event_id):
        if event_id:
            with self._lock:
                self._ignored.add(event_id)

    def time_until_drain(self, event):
        grace = timedelta(seconds=self.config.node_termination_grace_period)
        return (event.start_time - grace) - datetime.now(timezone.utc)

    def _should_drain(self, event):
        return (event.event_id not in self._ignored
                and not event.in_progress
                and not event.node_processed
                and self.time_until_drain(event) <= timedelta(0))

    def get_active_event(self):
        self._clean_periodically()
        with self._lock:
            for event in self._events.values():
                if self._should_drain(event):
                    return event
        return None

    def mark_all_as_processed(self, node_name):
        with self._lock:
            for event in self._events.values():
                if event.node_name == node_name:
                    event.node_processed = True

    def _clean_periodically(self):
        with self._lock:
            self._calls_since_last_clean += 1
            if self._calls_since_last_clean < self._cleaning_period:
                return
            log.info("garbage-collecting the interruption event store")
            for event_id in [e.event_id for e in self._events.values() if e.node_processed]:
                del self._events[event_id]
            self._calls_since_last_clean = 0
