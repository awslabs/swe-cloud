"""Task state store: per-task-instance state that outlives the worker process.

One DynamoDB item per task instance (see docs/state-store.md). Every try and every clear of the
task instance reads and writes the same item; other dag runs and map indexes have their own.
"""
from datetime import datetime, timezone
from typing import Any

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from durable_task import aws
from durable_task.ti import TaskInstance

_ser = TypeSerializer()
_de = TypeDeserializer()


class TaskStateStore:
    def __init__(self, table: str, ti: TaskInstance, client=None):
        self.table = table
        self.ti = ti
        self.ddb = client or aws.client("dynamodb")

    def _key(self) -> dict:
        return {"ti_key": {"S": self.ti.key}}

    def get_all(self) -> dict:
        resp = self.ddb.get_item(TableName=self.table, Key=self._key(), ConsistentRead=True)
        item = resp.get("Item") or {}
        return {k: _de.deserialize(v) for k, v in item.items()}

    def get(self, key: str, default: Any = None) -> Any:
        return self.get_all().get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.update({key: value})

    def update(self, values: dict) -> None:
        """Set several attributes in one write."""
        names = {f"#k{i}": k for i, k in enumerate(values)}
        exprs = {f":v{i}": _ser.serialize(v) for i, v in enumerate(values.values())}
        sets = ", ".join(f"#k{i} = :v{i}" for i in range(len(values)))
        self.ddb.update_item(
            TableName=self.table,
            Key=self._key(),
            UpdateExpression=f"SET {sets}, try_number = :t, updated_at = :u",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues={
                **exprs,
                ":t": {"N": str(self.ti.try_number)},
                ":u": {"S": datetime.now(timezone.utc).isoformat()},
            },
        )
