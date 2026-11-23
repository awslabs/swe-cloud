"""The VM fleet table: one item per VM, keyed by vm_id.

``state`` moves ``active`` -> ``destroying`` -> ``destroyed``. The capacity scheduler releases a VM's
quota once it reads ``destroyed``, so that state is written only after vm-destroy has exited 0.
Other services read ``state``, ``destroyed_by`` and ``destroyed_at``; extra attributes are ignored
by them.

Ownership of a teardown is a lease on the record: ``destroyer`` holds it under ``epoch`` until
``lease_until`` (epoch ms). A claim bumps the epoch and only succeeds on an expired lease; renewing
and finishing are conditioned on the claimer's epoch, so a worker that lost its lease cannot extend
it or overwrite its successor.
"""

import time

import boto3

STATE = {"#s": "state"}


def now_ms() -> int:
    return int(time.time() * 1000)


class Fleet:
    def __init__(self, table: str):
        self.table = table
        self.ddb = boto3.client("dynamodb")
        self.lost = self.ddb.exceptions.ConditionalCheckFailedException

    def get(self, vm_id: str):
        item = self.ddb.get_item(TableName=self.table, Key={"vm_id": {"S": vm_id}}, ConsistentRead=True).get("Item")
        if item is None:
            return None
        return {name: next(iter(value.values())) for name, value in item.items()}

    def _update(self, vm_id, expression, condition, values):
        try:
            self.ddb.update_item(
                TableName=self.table, Key={"vm_id": {"S": vm_id}}, UpdateExpression=expression,
                ConditionExpression=condition, ExpressionAttributeNames=STATE, ExpressionAttributeValues=values,
            )
            return True
        except self.lost:
            return False

    def claim(self, vm_id: str, worker_id: str, seen_epoch: int, lease_ms: int) -> bool:
        now = now_ms()
        epoch_matches = "attribute_not_exists(epoch)" if seen_epoch == 0 else "epoch = :seen"
        values = {
            ":destroying": {"S": "destroying"}, ":destroyed": {"S": "destroyed"}, ":w": {"S": worker_id},
            ":epoch": {"N": str(seen_epoch + 1)}, ":until": {"N": str(now + lease_ms)}, ":now": {"N": str(now)},
        }
        if seen_epoch:
            values[":seen"] = {"N": str(seen_epoch)}
        return self._update(
            vm_id,
            "SET #s = :destroying, destroyer = :w, epoch = :epoch, lease_until = :until, destroy_started_at = :now",
            f"attribute_exists(vm_id) AND #s <> :destroyed AND {epoch_matches} "
            "AND (attribute_not_exists(lease_until) OR lease_until < :now)",
            values,
        )

    def renew(self, vm_id: str, worker_id: str, epoch: int, lease_ms: int) -> bool:
        return self._update(
            vm_id, "SET lease_until = :until",
            "#s = :destroying AND destroyer = :w AND epoch = :epoch",
            {":until": {"N": str(now_ms() + lease_ms)}, ":destroying": {"S": "destroying"},
             ":w": {"S": worker_id}, ":epoch": {"N": str(epoch)}},
        )

    def mark_destroyed(self, vm_id: str, worker_id: str, epoch: int) -> bool:
        return self._update(
            vm_id, "SET #s = :destroyed, destroyed_by = :w, destroyed_at = :now REMOVE lease_until",
            "epoch = :epoch",
            {":destroyed": {"S": "destroyed"}, ":w": {"S": worker_id}, ":now": {"N": str(now_ms())}, ":epoch": {"N": str(epoch)}},
        )
