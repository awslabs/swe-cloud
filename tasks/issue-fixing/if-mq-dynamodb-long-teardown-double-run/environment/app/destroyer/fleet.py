"""The VM fleet table: one item per VM, keyed by vm_id.

``state`` moves ``active`` -> ``destroying`` -> ``destroyed``. The capacity scheduler releases a VM's
quota once it reads ``destroyed``, so that state is written only after vm-destroy has exited 0.
Other services read ``state``, ``destroyed_by`` and ``destroyed_at``; extra attributes are ignored
by them.
"""

import time

import boto3


def now_ms() -> int:
    return int(time.time() * 1000)


class Fleet:
    def __init__(self, table: str):
        self.table = table
        self.ddb = boto3.client("dynamodb")

    def get(self, vm_id: str):
        item = self.ddb.get_item(TableName=self.table, Key={"vm_id": {"S": vm_id}}, ConsistentRead=True).get("Item")
        if item is None:
            return None
        return {name: next(iter(value.values())) for name, value in item.items()}

    def mark_destroying(self, vm_id: str, worker_id: str) -> None:
        self.ddb.update_item(
            TableName=self.table,
            Key={"vm_id": {"S": vm_id}},
            UpdateExpression="SET #s = :s, destroyer = :w, destroy_started_at = :t",
            ExpressionAttributeNames={"#s": "state"},
            ExpressionAttributeValues={":s": {"S": "destroying"}, ":w": {"S": worker_id}, ":t": {"N": str(now_ms())}},
        )

    def mark_destroyed(self, vm_id: str, worker_id: str) -> None:
        self.ddb.update_item(
            TableName=self.table,
            Key={"vm_id": {"S": vm_id}},
            UpdateExpression="SET #s = :s, destroyed_by = :w, destroyed_at = :t",
            ExpressionAttributeNames={"#s": "state"},
            ExpressionAttributeValues={":s": {"S": "destroyed"}, ":w": {"S": worker_id}, ":t": {"N": str(now_ms())}},
        )
