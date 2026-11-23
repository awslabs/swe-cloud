"""SQS FIFO publisher. One message per outbox row, sent in the order given."""
from __future__ import annotations

import boto3

from .store import OutboxRow


class PublishError(RuntimeError):
    pass


class Publisher:
    def __init__(self, queue_url: str, group_id: str):
        self.queue_url = queue_url
        self.group_id = group_id
        self.sqs = boto3.client("sqs")

    def publish(self, rows: list[OutboxRow]) -> None:
        for i in range(0, len(rows), 10):
            chunk = rows[i:i + 10]
            resp = self.sqs.send_message_batch(
                QueueUrl=self.queue_url,
                Entries=[
                    {
                        "Id": str(n),
                        "MessageBody": row.data,
                        "MessageGroupId": self.group_id,
                        # the queue drops a resend of the same id within its 5-minute window
                        "MessageDeduplicationId": row.message_id,
                        "MessageAttributes": {
                            "position": {"DataType": "Number", "StringValue": str(row.position)},
                            "message_id": {"DataType": "String", "StringValue": row.message_id},
                            "message_type": {"DataType": "String", "StringValue": row.message_type},
                        },
                    }
                    for n, row in enumerate(chunk)
                ],
            )
            if resp.get("Failed"):
                raise PublishError(f"send failed for {resp['Failed']}")
