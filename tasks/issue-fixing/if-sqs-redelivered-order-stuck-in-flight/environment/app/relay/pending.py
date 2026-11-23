import threading
from collections import deque


class PendingLock:
    """The lock a dispatched delivery is settled through.

    One per in-flight MessageId: `current` is the delivery being handled; later deliveries of the
    same message wait in `pending`."""

    def __init__(self):
        self._mutex = threading.Lock()
        self._current = None
        self._pending = deque()

    @property
    def is_empty(self):
        return self._current is None and not self._pending

    def complete(self):
        self._execute(lambda lease: lease.complete(), clear=True)

    def faulted(self, exception):
        self._execute(lambda lease: lease.faulted(exception), clear=True)

    def validate(self):
        self._execute(lambda lease: lease.validate())

    def enqueue(self, context, lease):
        """True when this delivery becomes the current one and should be handled."""
        with self._mutex:
            if self._current is None and not self._pending:
                self._current = (context, lease)
                return True
            self._pending.append((context, lease))
            return False

    def _execute(self, action, clear=False):
        if self._current is None:
            with self._mutex:
                if self._current is None:
                    if not self._pending:
                        return
                    self._current = self._pending.popleft()

        error = None
        while True:
            try:
                _, lease = self._current
                if clear:
                    self._current = None
                action(lease)
                return
            except Exception as exc:
                error = exc
            if not self._try_dequeue():
                break

        if error is not None:
            raise error

    def _try_dequeue(self):
        with self._mutex:
            if not self._pending:
                self._current = None
                return False
            self._current = self._pending.popleft()
            return True

    def cancel(self):
        with self._mutex:
            if self._current is not None:
                self._current[0].cancel()
            for context, _ in self._pending:
                context.cancel()
