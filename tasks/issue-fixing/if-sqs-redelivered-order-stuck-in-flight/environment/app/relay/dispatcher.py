import logging
import time

log = logging.getLogger("relay.dispatch")


class ReceiveDispatcher:
    """Runs the handler for one delivery and settles it through `lock`."""

    def __init__(self, handler):
        self._handler = handler

    def dispatch(self, context, lock):
        started = time.monotonic()
        try:
            lock.validate()
            self._handler(context)
            lock.complete()
            log.info("OK %s %.1fs", context.message_id, time.monotonic() - started)
        except Exception as exc:
            log.error("FAULT %s %.1fs: %s", context.message_id, time.monotonic() - started, exc)
            try:
                lock.faulted(exc)
            except Exception as release_error:
                raise RuntimeError(f"faulted() raised while settling {context.message_id}") from release_error
            raise
