"""One received SQS message, seen as a job delivery."""

import sys


class SqsJob:
    def __init__(self, client, queue_url, message):
        self.client = client
        self.queue_url = queue_url
        self.message = message
        self.released = False
        self.deleted = False
        self.failed = False

    @property
    def message_id(self):
        return self.message["MessageId"]

    @property
    def body(self):
        return self.message["Body"]

    def attempts(self):
        """How many times this message has been delivered, this delivery included."""
        return int(self.message["Attributes"]["ApproximateReceiveCount"])

    def release(self, delay):
        """Put the message back: it becomes visible again after ``delay`` seconds."""
        self.client.change_message_visibility(QueueUrl=self.queue_url,
                                              ReceiptHandle=self.message["ReceiptHandle"],
                                              VisibilityTimeout=delay)
        self.released = True

    def delete(self):
        self.client.delete_message(QueueUrl=self.queue_url,
                                   ReceiptHandle=self.message["ReceiptHandle"])
        self.deleted = True

    def fail(self, reason):
        self.failed = True
        print(f"job failed message_id={self.message_id} attempts={self.attempts()} "
              f"reason={reason}", file=sys.stderr, flush=True)
        self.delete()

    def is_deleted_or_released(self):
        return self.deleted or self.released
