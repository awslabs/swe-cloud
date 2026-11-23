"""One received SQS message on its way through the relay."""

import itertools
import logging

LOG = logging.getLogger(__name__)

_ids = itertools.count(1)


class Delivery:

    def __init__(self, message):
        self.delivery_id = f"d-{next(_ids)}"
        self.message_id = message["MessageId"]
        self.receipt_handle = message["ReceiptHandle"]
        self.body = message.get("Body", "")
        self.attributes = message.get("Attributes", {})
        self.batch_index = None
        self.batch_size = None
        self.failure = None
        self._on_completion = []

    def add_on_completion(self, on_complete, on_failure):
        self._on_completion.append((on_complete, on_failure))

    def done(self, failure=None):
        """Runs the completion callbacks, the last registered first."""
        self.failure = failure
        for on_complete, on_failure in reversed(self._on_completion):
            try:
                if failure is None:
                    on_complete(self)
                else:
                    on_failure(self)
            except Exception:
                LOG.exception("completion callback failed for %s", self.message_id)
        self._on_completion.clear()

    def __repr__(self):
        return f"Delivery({self.delivery_id}, message_id={self.message_id})"
