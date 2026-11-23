import logging
import threading
import time

from .sqs import MessageNotInflight, ReceiptHandleIsInvalid, SqsError

log = logging.getLogger("relay.lease")

# SQS caps a message's visibility at 12 hours from its first receive.
MAX_VISIBILITY_SECONDS = 12 * 60 * 60


class LockLost(Exception):
    pass


class DeliveryLease:
    """The visibility lease of one delivery (one receipt handle).

    A heartbeat thread starts as soon as the lease exists and keeps the delivery hidden until the
    lease is completed (deleted) or faulted (returned to the queue)."""

    def __init__(self, message, settings, client):
        self.message = message
        self.message_id = message["MessageId"]
        self.receipt_handle = message["ReceiptHandle"]
        self._settings = settings
        self._client = client
        self._started_at = time.monotonic()
        self._cancel = threading.Event()
        self._locked = True
        self._heartbeat = threading.Thread(
            target=self._renew, name=f"heartbeat-{self.message_id[:8]}", daemon=True
        )
        self._heartbeat.start()

    def complete(self):
        self._cancel.set()
        try:
            self._client.delete(self.receipt_handle)
            self._heartbeat.join()
        except (MessageNotInflight, ReceiptHandleIsInvalid):
            self._locked = False
            raise

    def faulted(self, exception):
        self._cancel.set()
        try:
            self._heartbeat.join()
            self._client.change_visibility(self.receipt_handle, self._settings.redeliver_visibility_timeout)
            self._locked = False
        except (MessageNotInflight, ReceiptHandleIsInvalid):
            self._locked = False
            raise
        except Exception as error:
            log.error("ChangeMessageVisibility failed: %s (original: %r): %s", self.receipt_handle, exception, error)

    def abandon(self):
        """Stop the heartbeat without settling: this handle no longer acts on the message."""
        self._cancel.set()
        if self._heartbeat is not threading.current_thread():
            self._heartbeat.join()

    def validate(self):
        if not self._locked:
            raise LockLost(f"message lock lost: {self.receipt_handle}")

    def _renew(self):
        timeout = self._settings.visibility_timeout
        delay = timeout * 0.7
        timeout = min(60, timeout)

        while self._locked and not self._cancel.is_set():
            try:
                if delay > 0 and self._cancel.wait(delay):
                    break
                if self._cancel.is_set():
                    break

                # An extension counts from when SQS gets it, not from when its answer comes back:
                # schedule the next one from the send, or a slow answer eats the margin.
                sent = time.monotonic()
                self._client.change_visibility(self.receipt_handle, timeout)

                if time.monotonic() - self._started_at + timeout >= MAX_VISIBILITY_SECONDS:
                    break

                delay = max(0.0, sent + timeout * 0.7 - time.monotonic())
            except MessageNotInflight:
                log.warning("message no longer in flight: %s", self.message_id)
                self._locked = False
                break
            except ReceiptHandleIsInvalid:
                log.warning("receipt handle is invalid: %s", self.message_id)
                self._locked = False
                break
            except SqsError as error:
                log.error("failed to extend visibility of %s to %ss: %s", self.message_id, timeout, error)
                break
            except TimeoutError:
                delay = 0
            except Exception:
                break
