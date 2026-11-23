"""Keeps the messages the relay holds invisible to the other instances.

One fixed-rate task (period ``max(1, V / 2)`` seconds) sends ``ChangeMessageVisibilityBatch``
(``VisibilityTimeout = V``) for every registered message whose deadline falls before the next run.
The deadline is 80% of the visibility timeout, which leaves the rest of the window for the call
itself and for scheduling.
"""

import collections
import logging
import threading
import time

from botocore.exceptions import BotoCoreError, ClientError

LOG = logging.getLogger(__name__)

RECEIPT_HANDLE_IS_INVALID = "ReceiptHandleIsInvalid"
MAX_REQUESTS = 10
DEADLINE_FRACTION = 0.8


class VisibilityExtender:

    def __init__(self, client, queue_url, visibility_timeout, delay_between_executions, should_extend=None):
        self.client = client
        self.queue_url = queue_url
        self.visibility_timeout = visibility_timeout
        self.delay_between_executions = delay_between_executions
        # whether the relay still intends to finish the message of an delivery id
        self.should_extend = should_extend or (lambda delivery_id: True)
        self._run = True
        self._lock = threading.Lock()
        self.entries = {}

    def add(self, delivery, received):
        """Starts extending the message of ``delivery``.

        ``received`` is the monotonic time SQS started its visibility timeout, or an earlier time.
        Returns True if the message must be extended before the next scheduled run.
        """
        delivery.add_on_completion(self._removed, self._removed)
        request = {
            "Id": delivery.delivery_id,
            "ReceiptHandle": delivery.receipt_handle,
            "VisibilityTimeout": self.visibility_timeout,
        }
        with self._lock:
            entry = ExtenderEntry(request, received)
            self.entries[delivery.delivery_id] = entry
        return entry.is_deadline_reached_at(time.monotonic() + max(1, self.delay_between_executions))

    def _removed(self, delivery):
        LOG.debug("removing %s from the extender, processing done", delivery.delivery_id)
        self.remove(delivery)

    def remove(self, delivery):
        """Stops extending the message of ``delivery``. The message is not made visible: it becomes
        visible again when its current (possibly just extended) visibility timeout runs out."""
        with self._lock:
            self.entries.pop(delivery.delivery_id, None)

    def clear(self):
        with self._lock:
            self.entries.clear()

    def cancel(self):
        self._run = False

    def run(self):
        if not self._run:
            return
        next_expected_execution = time.monotonic() + max(1, self.delay_between_executions)
        with self._lock:
            entry_queue = collections.deque(self.entries.values())

        while entry_queue:
            batch_entries = []
            # up to 10 entries per ChangeMessageVisibilityBatch request
            while entry_queue and len(batch_entries) < MAX_REQUESTS:
                next_entry = entry_queue.popleft()
                if (next_entry.is_deadline_reached_at(next_expected_execution)
                        and self.should_extend(next_entry.extend_request["Id"])):
                    batch_entries.append(next_entry.extend_request)
            if not batch_entries:
                continue
            try:
                LOG.debug("extending visibility by %s s for %s", self.visibility_timeout,
                          [e["Id"] for e in batch_entries])
                # SQS counts the new visibility timeout from the call, so the next deadline is counted from
                # before the call was sent
                sent = time.monotonic()
                response = self.client.change_message_visibility_batch(
                    QueueUrl=self.queue_url, Entries=batch_entries)
                for failed in response.get("Failed", []):
                    if failed.get("Code") == RECEIPT_HANDLE_IS_INVALID:
                        LOG.debug("extending visibility failed with invalid handle: %s", failed)
                    else:
                        LOG.warning("extending visibility failed: %s", failed)
                for successful in response.get("Successful", []):
                    LOG.debug("extended visibility for %s", successful["Id"])
                    with self._lock:
                        current = self.entries.get(successful["Id"])
                        if current is not None:
                            self.entries[successful["Id"]] = current.extend_deadline(sent)
            except (BotoCoreError, ClientError) as error:
                LOG.warning("extending visibility failed for %s (tried again on the next run): %s",
                            [e["Id"] for e in batch_entries], error)


class ExtenderEntry:

    __slots__ = ("deadline", "extend_request")

    def __init__(self, extend_request, start):
        # deadline at 80% of the visibility timeout after it started (the receive or the last extension call),
        # to account for processing time
        self.deadline = start + extend_request["VisibilityTimeout"] * DEADLINE_FRACTION
        self.extend_request = extend_request

    def extend_deadline(self, start):
        return ExtenderEntry(self.extend_request, start)

    def is_deadline_reached_at(self, when):
        return self.deadline < when
