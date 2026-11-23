"""Task records for the delayed-task runner.

One row per task identity, keyed by `pk`. The row is the shared record of
what the task is, who is running it, and how many times its effect has been
committed. `effect_count` is a counter: it is only ever incremented, so a
task that runs twice reads as 2, not as 1.
"""

import time

import boto3
from botocore.config import Config

import settings

PENDING = "PENDING"
DONE = "DONE"


def task_key(run_id, task_id):
    return "RUN#%s#TASK#%s" % (run_id, task_id)


class Table:
    def __init__(self, name, endpoint, region):
        self.name = name
        self.client = boto3.client(
            "dynamodb",
            endpoint_url=endpoint,
            region_name=region,
            config=Config(
                connect_timeout=settings.API_TIMEOUT_S,
                read_timeout=settings.API_TIMEOUT_S,
                retries={"max_attempts": settings.API_ATTEMPTS},
            ),
        )

    def record_task(self, run_id, task_id, message_id, due_at, holder):
        """Write this task's identity and current holder onto its row."""
        self.client.update_item(
            TableName=self.name,
            Key={"pk": {"S": task_key(run_id, task_id)}},
            UpdateExpression=(
                "SET task_id = :tid, message_id = :mid, due_at = :due,"
                " holder = :who, phase = :phase"
            ),
            ExpressionAttributeValues={
                ":tid": {"S": task_id},
                ":mid": {"S": message_id},
                ":due": {"N": repr(float(due_at))},
                ":who": {"S": holder},
                ":phase": {"S": PENDING},
            },
        )

    def commit_effect(self, run_id, task_id, holder):
        """Commit the task's logical effect: one increment of the counter.

        Called from the task's own callback when it runs.
        """
        now = repr(float(time.time()))
        self.client.update_item(
            TableName=self.name,
            Key={"pk": {"S": task_key(run_id, task_id)}},
            UpdateExpression=(
                "ADD effect_count :one SET done_at = :now, phase = :done"
            ),
            ExpressionAttributeValues={
                ":one": {"N": "1"},
                ":now": {"N": now},
                ":done": {"S": DONE},
            },
        )
        return float(now)

    def get_task(self, run_id, task_id):
        resp = self.client.get_item(
            TableName=self.name,
            Key={"pk": {"S": task_key(run_id, task_id)}},
            ConsistentRead=True,
        )
        item = resp.get("Item")
        if not item:
            return None
        return {
            "task_id": item["task_id"]["S"],
            "message_id": item.get("message_id", {}).get("S"),
            "due_at": float(item["due_at"]["N"]),
            "holder": item.get("holder", {}).get("S"),
            "phase": item.get("phase", {}).get("S"),
            "effect_count": int(item.get("effect_count", {"N": "0"})["N"]),
            "done_at": (
                float(item["done_at"]["N"]) if "done_at" in item else None
            ),
        }
