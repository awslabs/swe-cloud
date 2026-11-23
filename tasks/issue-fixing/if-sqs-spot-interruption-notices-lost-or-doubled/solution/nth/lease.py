"""Visibility leases on the messages this replica holds.

The queue is shared by every replica of every cluster and by the batch fleet's handler; a
message's visibility is the only thing that keeps the others away from it. A replica holds a
short lease on each message it has received and renews it from its own thread for as long as it
works the message, so a long drain never lets the message reappear while this replica lives, and
a replica that dies stops renewing and the message comes back within one lease. A message the
replica will not work is handed back at once.
"""

import logging
import threading

log = logging.getLogger("nth.lease")


class LeaseKeeper:
    def __init__(self, sqs, queue_url, lease_sec, renew_every_sec):
        self.sqs = sqs
        self.queue_url = queue_url
        self.lease_sec = lease_sec
        self.renew_every_sec = renew_every_sec
        self._held = {}  # MessageId -> latest receipt handle
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._renew_loop, daemon=True, name="lease-renewer")
        self._thread.start()

    def hold(self, message):
        """Start (or keep) renewing the message's lease. False if it was already held."""
        with self._lock:
            already = message["MessageId"] in self._held
            self._held[message["MessageId"]] = message["ReceiptHandle"]
        return not already

    def _take(self, message):
        with self._lock:
            return self._held.pop(message["MessageId"], None) or message["ReceiptHandle"]

    def release(self, message, visibility_sec=0):
        """Stop renewing and make the message receivable again after visibility_sec."""
        handle = self._take(message)
        try:
            self.sqs.change_message_visibility(QueueUrl=self.queue_url, ReceiptHandle=handle,
                                               VisibilityTimeout=visibility_sec)
        except Exception as err:  # noqa: BLE001
            log.warning("could not release message %s: %s", message["MessageId"], err)

    def delete(self, message):
        """Stop renewing and delete the message with the most recent receipt handle."""
        handle = self._take(message)
        self.sqs.delete_message(QueueUrl=self.queue_url, ReceiptHandle=handle)

    def stop(self):
        self._stop.set()

    def _renew_loop(self):
        while not self._stop.wait(self.renew_every_sec):
            with self._lock:
                message_ids = list(self._held)
            for message_id in message_ids:
                # Renew under the lock so a release or delete never runs between our check
                # and the call (a late renewal would hide a released message again).
                with self._lock:
                    handle = self._held.get(message_id)
                    if handle is None:
                        continue
                    try:
                        self.sqs.change_message_visibility(
                            QueueUrl=self.queue_url, ReceiptHandle=handle,
                            VisibilityTimeout=self.lease_sec)
                    except Exception as err:  # noqa: BLE001
                        log.warning("could not renew lease on %s: %s", message_id, err)
