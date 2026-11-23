"""Expiring named locks stored as DynamoDB items.

One item per lock name: ``pk`` is the name, ``owner`` the random token of whoever acquired it and
``expires_at`` (epoch seconds) when it lapses. An expired item counts as free: the next acquire
overwrites it. There is no DynamoDB TTL on these tables; expiry is only ever compared.
"""

import secrets
import time

from botocore.exceptions import ClientError

FOREVER = 86400


def _conditional_failed(error):
    return error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


class LockStore:
    def __init__(self, client, table):
        self.client = client
        self.table = table

    def lock(self, name, seconds=0, owner=None):
        return Lock(self, name, seconds, owner)

    def restore(self, name, owner):
        """A handle on a lock some earlier process acquired as ``owner``."""
        return Lock(self, name, 0, owner)

    def get(self, name):
        item = self.client.get_item(TableName=self.table, Key={"pk": {"S": name}},
                                    ConsistentRead=True).get("Item")
        if not item:
            return None
        return {"owner": item["owner"]["S"], "expires_at": int(item["expires_at"]["N"])}


class Lock:
    def __init__(self, store, name, seconds, owner=None):
        self.store = store
        self.name = name
        self.seconds = seconds
        self.owner = owner or secrets.token_hex(16)

    def acquire(self):
        now = int(time.time())
        seconds = self.seconds if self.seconds > 0 else FOREVER
        try:
            self.store.client.put_item(
                TableName=self.store.table,
                Item={
                    "pk": {"S": self.name},
                    "owner": {"S": self.owner},
                    "expires_at": {"N": str(now + seconds)},
                },
                ConditionExpression="attribute_not_exists(pk) OR expires_at < :now",
                ExpressionAttributeValues={":now": {"N": str(now)}},
            )
            return True
        except ClientError as error:
            if _conditional_failed(error):
                return False
            raise

    def release(self):
        """Delete the lock only if it is still held by this owner."""
        try:
            self.store.client.delete_item(
                TableName=self.store.table,
                Key={"pk": {"S": self.name}},
                ConditionExpression="#owner = :owner",
                ExpressionAttributeNames={"#owner": "owner"},
                ExpressionAttributeValues={":owner": {"S": self.owner}},
            )
            return True
        except ClientError as error:
            if _conditional_failed(error):
                return False
            raise

    def force_release(self):
        """Delete the lock whoever holds it."""
        self.store.client.delete_item(TableName=self.store.table, Key={"pk": {"S": self.name}})
