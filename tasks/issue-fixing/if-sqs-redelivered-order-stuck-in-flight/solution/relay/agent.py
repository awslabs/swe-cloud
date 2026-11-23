import logging
import threading

log = logging.getLogger("relay.agent")


class InFlight:
    """Every delivery of one MessageId received while a handler call for it is running.

    Only the newest delivery's receipt handle still acts on the message, so the newest lease is the
    one that must stay renewed and the one the outcome is applied through."""

    def __init__(self, lease):
        self.leases = [lease]


class InFlightLock:
    """What the dispatcher settles a delivery through."""

    def __init__(self, agent, key, entry):
        self._agent = agent
        self._key = key
        self._entry = entry

    def validate(self):
        self._entry.leases[-1].validate()

    def complete(self):
        self._agent.settle(self._key, self._entry, lambda lease: lease.complete())

    def faulted(self, exception):
        self._agent.settle(self._key, self._entry, lambda lease: lease.faulted(exception))


class ConsumerAgent:
    """Hands deliveries to the dispatcher, at most one delivery per MessageId at a time."""

    def __init__(self, dispatcher, queue_name):
        self._dispatcher = dispatcher
        self._queue_name = queue_name
        self._mutex = threading.Lock()
        self._pending = {}

    def dispatch(self, key, context, lease):
        superseded = None
        with self._mutex:
            entry = self._pending.get(key)
            if entry is not None:
                superseded = entry.leases[-1]
                entry.leases.append(lease)
            else:
                entry = InFlight(lease)
                self._pending[key] = entry

        if superseded is not None:
            # The new delivery's handle is now the message's lease; the older handle no longer acts.
            superseded.abandon()
            log.warning("DUPLICATE %s %s receive_count=%s", self._queue_name, key, context.receive_count)
            return

        try:
            self._dispatcher.dispatch(context, InFlightLock(self, key, entry))
        finally:
            with self._mutex:
                if self._pending.get(key) is entry:
                    del self._pending[key]

    def settle(self, key, entry, action):
        # Forget the entry before the outcome reaches SQS: a release makes the message receivable
        # at once, and that delivery must be dispatched as a new first copy, not parked here.
        with self._mutex:
            if self._pending.get(key) is entry:
                del self._pending[key]
            leases = list(entry.leases)
        for lease in leases[:-1]:
            lease.abandon()
        action(leases[-1])

    def cancel_pending(self):
        with self._mutex:
            entries = list(self._pending.values())
            self._pending.clear()
        for entry in entries:
            for lease in entry.leases:
                lease.abandon()
