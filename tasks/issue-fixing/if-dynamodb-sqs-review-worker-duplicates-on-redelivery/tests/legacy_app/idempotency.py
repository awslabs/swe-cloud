"""Handler-level idempotency records, after the Lambda Powertools pattern.

One record per idempotency key. While the handler runs the record is
INPROGRESS; afterwards it is COMPLETE and holds the handler's response, which
is returned to any repeat of the same key. A handler that raises deletes its
record so the event can be retried. A handler that never finishes (the worker
died) leaves INPROGRESS behind; once `in_progress_expiry` has passed, the next
attempt treats the record as expired and runs the handler again.
"""
import json
import time

from botocore.exceptions import ClientError

INPROGRESS = "INPROGRESS"
COMPLETE = "COMPLETE"


class AlreadyInProgress(Exception):
    """Another attempt holds an unexpired INPROGRESS record for this key."""


class IdempotencyStore:
    def __init__(self, ddb, table, in_progress_expiry_s=4.0, owner=""):
        # Handlers finish in well under a second, so a few seconds is enough
        # to tell a crashed attempt from a running one.
        self.ddb = ddb
        self.table = table
        self.in_progress_expiry_s = in_progress_expiry_s
        self.owner = owner

    def ensure_table(self):
        try:
            self.ddb.create_table(
                TableName=self.table,
                KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
        except ClientError as err:
            if err.response["Error"]["Code"] != "ResourceInUseException":
                raise

    def run(self, key, handler):
        """Run `handler()` at most once per key, returning its (stored) response."""
        now = time.time()
        try:
            self.ddb.put_item(
                TableName=self.table,
                Item={
                    "id": {"S": key},
                    "status": {"S": INPROGRESS},
                    "in_progress_expiry": {"N": repr(now + self.in_progress_expiry_s)},
                    "owner": {"S": self.owner},
                },
                ConditionExpression="attribute_not_exists(id) OR (#status = :inprogress AND in_progress_expiry < :now)",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":inprogress": {"S": INPROGRESS}, ":now": {"N": repr(now)}},
            )
        except ClientError as err:
            if err.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            record = self.ddb.get_item(
                TableName=self.table, Key={"id": {"S": key}}, ConsistentRead=True
            ).get("Item")
            if record and record["status"]["S"] == COMPLETE:
                return json.loads(record["response"]["S"])
            raise AlreadyInProgress(key)

        try:
            response = handler()
        except Exception:
            self.ddb.delete_item(TableName=self.table, Key={"id": {"S": key}})
            raise
        self.ddb.put_item(
            TableName=self.table,
            Item={
                "id": {"S": key},
                "status": {"S": COMPLETE},
                "response": {"S": json.dumps(response)},
                "owner": {"S": self.owner},
            },
        )
        return response
