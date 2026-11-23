import logging
import threading

from .pending import PendingLock

log = logging.getLogger("relay.agent")


class ConsumerAgent:
    """Hands deliveries to the dispatcher, at most one delivery per MessageId at a time."""

    def __init__(self, dispatcher, queue_name):
        self._dispatcher = dispatcher
        self._queue_name = queue_name
        self._mutex = threading.Lock()
        self._pending = {}

    def dispatch(self, key, context, lease):
        with self._mutex:
            pending = self._pending.get(key)
            if pending is None:
                pending = PendingLock()
                self._pending[key] = pending
            added = pending.enqueue(context, lease)

        if not added:
            log.warning("DUPLICATE %s %s receive_count=%s", self._queue_name, key, context.receive_count)
            return

        try:
            self._dispatcher.dispatch(context, pending)
        finally:
            with self._mutex:
                value = self._pending.get(key)
                if value is not None and value.is_empty:
                    del self._pending[key]

    def cancel_pending(self):
        with self._mutex:
            entries = list(self._pending.items())
            self._pending.clear()
        for _, pending in entries:
            pending.cancel()
