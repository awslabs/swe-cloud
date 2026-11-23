"""Queue access for the delayed-task runner."""

import hashlib
import json

import boto3
from botocore.config import Config

import settings


def digest(handle):
    return hashlib.sha256(handle.encode("utf-8")).hexdigest()[:12]


class Queue:
    def __init__(self, queue_url, endpoint, region):
        self.queue_url = queue_url
        self.client = boto3.client(
            "sqs",
            endpoint_url=endpoint,
            region_name=region,
            config=Config(
                connect_timeout=settings.API_TIMEOUT_S,
                read_timeout=settings.API_TIMEOUT_S,
                retries={"max_attempts": settings.API_ATTEMPTS},
            ),
        )

    def receive(self, count=1, wait=0):
        """Return raw deliveries, at most `count`, without waiting."""
        resp = self.client.receive_message(
            QueueUrl=self.queue_url,
            MaxNumberOfMessages=count,
            WaitTimeSeconds=wait,
            AttributeNames=["All"],
            MessageAttributeNames=["All"],
        )
        return resp.get("Messages", []) or []

    def send(self, body):
        resp = self.client.send_message(QueueUrl=self.queue_url, MessageBody=body)
        return resp.get("MessageId")

    def hide_for(self, handle, seconds):
        self.client.change_message_visibility(
            QueueUrl=self.queue_url,
            ReceiptHandle=handle,
            VisibilityTimeout=int(seconds),
        )

    def remove(self, handle):
        self.client.delete_message(QueueUrl=self.queue_url, ReceiptHandle=handle)

    def attributes(self):
        resp = self.client.get_queue_attributes(
            QueueUrl=self.queue_url,
            AttributeNames=[
                "VisibilityTimeout",
                "DelaySeconds",
                "MessageRetentionPeriod",
                "RedrivePolicy",
                "ApproximateNumberOfMessages",
            ],
        )
        return resp.get("Attributes", {})


def decode(delivery):
    """Return the task body of a raw delivery."""
    return json.loads(delivery["Body"])
