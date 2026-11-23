"""Checkpoint record client (DynamoDB).

One table holds one item per log.  The item is

    logID       (B)   32-byte log identifier, the partition key
    checkpoint  (B)   the complete checkpoint bytes

and nothing else.  The table is the recovery authority for a log: the
record it holds is the latest committed checkpoint, reads are strongly
consistent, and a replacement is only allowed when the stored bytes are
still exactly the bytes the writer saved when it read the record.
"""

import boto3
from botocore.config import Config


class ConflictError(Exception):
    """The stored checkpoint is not the one this writer expected."""


class LockDb:
    def __init__(self, config):
        self.table = config["table"]
        self.log_id = bytes.fromhex(config["log_id"])
        self.client = boto3.client(
            "dynamodb",
            endpoint_url=config["ddb_endpoint"],
            config=Config(retries={"max_attempts": 1}, read_timeout=120,
                          connect_timeout=10),
        )

    def fetch(self):
        """Return the current checkpoint bytes, or None if absent."""
        resp = self.client.get_item(
            TableName=self.table,
            Key={"logID": {"B": self.log_id}},
            ConsistentRead=True,
        )
        item = resp.get("Item")
        if not item:
            return None
        return item["checkpoint"]["B"]

    def create(self, checkpoint: bytes):
        """Install the first checkpoint for the log."""
        self.client.put_item(
            TableName=self.table,
            Item={
                "logID": {"B": self.log_id},
                "checkpoint": {"B": checkpoint},
            },
            ConditionExpression="attribute_not_exists(logID)",
        )

    def replace(self, old: bytes, new: bytes):
        """Advance the record, but only if it still holds exactly `old`."""
        try:
            self.client.put_item(
                TableName=self.table,
                Item={
                    "logID": {"B": self.log_id},
                    "checkpoint": {"B": new},
                },
                ConditionExpression="checkpoint = :old",
                ExpressionAttributeValues={":old": {"B": old}},
            )
        except self.client.exceptions.ConditionalCheckFailedException:
            raise ConflictError("stored checkpoint moved on")

    def raw_client(self):
        return self.client
