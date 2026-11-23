import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .agent import ConsumerAgent
from .dispatcher import ReceiveDispatcher
from .lease import DeliveryLease
from .sqs import SqsError

log = logging.getLogger("relay.receiver")


class ReceiveContext:
    def __init__(self, message):
        self.message = message
        self.message_id = message["MessageId"]
        self.body = message.get("Body", "")
        attributes = message.get("Attributes", {})
        self.receive_count = int(attributes.get("ApproximateReceiveCount", "1"))
        self.redelivered = self.receive_count > 1
        self.cancelled = threading.Event()

    def cancel(self):
        self.cancelled.set()


class MessageReceiver:
    """Receive loop: keeps up to `concurrency` deliveries in the handler at once."""

    def __init__(self, settings, client, handler):
        self._settings = settings
        self._client = client
        self._stopping = threading.Event()
        self._slots = threading.BoundedSemaphore(settings.concurrency)
        self._executor = ThreadPoolExecutor(max_workers=settings.concurrency, thread_name_prefix="handler")
        queue_name = settings.queue_url.rstrip("/").rsplit("/", 1)[-1]
        self._agent = ConsumerAgent(ReceiveDispatcher(handler), queue_name)

    def stop(self):
        self._stopping.set()

    def run(self):
        self._read_queue_attributes()
        log.info(
            "receiving from %s (visibility %ss, concurrency %s)",
            self._settings.queue_url, self._settings.visibility_timeout, self._settings.concurrency,
        )
        while not self._stopping.is_set():
            if not self._slots.acquire(timeout=0.5):
                continue
            taken = 1
            while taken < min(10, self._settings.concurrency) and self._slots.acquire(blocking=False):
                taken += 1
            try:
                messages = self._client.receive(taken, self._settings.wait_time_seconds)
            except SqsError as error:
                log.error("receive failed: %s", error)
                messages = []
                time.sleep(1)
            except Exception as error:
                log.error("receive failed: %r", error)
                messages = []
                time.sleep(1)
            for message in messages:
                self._executor.submit(self._handle, message)
            for _ in range(taken - len(messages)):
                self._slots.release()

        self._agent.cancel_pending()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _read_queue_attributes(self):
        attributes = self._client.queue_attributes()
        value = attributes.get("VisibilityTimeout")
        if value is not None and int(value) != self._settings.visibility_timeout:
            self._settings.visibility_timeout = int(value)

    def _handle(self, message):
        try:
            lease = DeliveryLease(message, self._settings, self._client)
            self._handle_message(message, lease)
        except Exception:
            log.exception("handler slot crashed")
        finally:
            self._slots.release()

    def _handle_message(self, message, lease):
        if self._stopping.is_set():
            return
        context = ReceiveContext(message)
        try:
            self._agent.dispatch(message["MessageId"], context, lease)
        except Exception as exc:
            log.warning("T-FAULT %s: %s", context.message_id, exc)
