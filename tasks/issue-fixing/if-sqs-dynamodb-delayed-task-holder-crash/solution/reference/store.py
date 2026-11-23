"""Task records for the delayed-task runner.

Same row shape as the fleet's table, plus the ownership bookkeeping this
implementation needs: `epoch` counts takeovers of a task and `lease_until`
is when the current holder's claim runs out. Both belong to the row because
both must survive the holder.
"""

import time

import boto3
from botocore.config import Config

import settings

PENDING = "PENDING"
DONE = "DONE"

LEASE_S = 4.0


def task_key(run_id, task_id):
    return "RUN#%s#TASK#%s" % (run_id, task_id)


def _num(value):
    return {"N": repr(float(value))}


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

    def _conditional(self, **kwargs):
        try:
            self.client.update_item(**kwargs)
            return True
        except self.client.exceptions.ConditionalCheckFailedException:
            return False

    def claim(self, run_id, task_id, message_id, due_at, holder):
        """Become the holder of this task, or return None.

        A claim succeeds when the task has never been held, when it is being
        taken back by its current holder, or when the previous holder's lease
        has run out. A finished task is never claimed.
        """
        deadline = time.time() + 1.5
        while True:
            ok = self._conditional(
                TableName=self.name,
                Key={"pk": {"S": task_key(run_id, task_id)}},
                UpdateExpression=(
                    "SET task_id = :tid, message_id = :mid, due_at = :due,"
                    " holder = :who, phase = :phase, lease_until = :lease"
                    " ADD epoch :one"
                ),
                ConditionExpression=(
                    "attribute_not_exists(pk) OR ("
                    " phase = :pending AND ("
                    "  attribute_not_exists(holder) OR holder = :who OR"
                    "  attribute_not_exists(lease_until) OR lease_until < :now))"
                ),
                ExpressionAttributeValues={
                    ":tid": {"S": task_id},
                    ":mid": {"S": message_id},
                    ":due": _num(due_at),
                    ":who": {"S": holder},
                    ":phase": {"S": PENDING},
                    ":lease": _num(time.time() + LEASE_S),
                    ":one": {"N": "1"},
                    ":pending": {"S": PENDING},
                    ":now": _num(time.time()),
                },
            )
            if ok:
                row = self.get_task(run_id, task_id)
                return row["epoch"] if row else None
            row = self.get_task(run_id, task_id)
            if row and row["phase"] == DONE:
                return None
            if (
                row
                and row["holder"] not in (None, holder)
                and (row["lease_until"] or 0) > deadline
            ):
                return None       # its holder's claim outlasts our patience
            if time.time() >= deadline:
                return None
            time.sleep(0.2)

    def renew(self, run_id, task_id, holder, epoch):
        """Extend this holder's lease, or report that it is no longer ours."""
        return self._conditional(
            TableName=self.name,
            Key={"pk": {"S": task_key(run_id, task_id)}},
            UpdateExpression="SET lease_until = :lease",
            ConditionExpression=(
                "holder = :who AND epoch = :ep AND phase = :pending"
            ),
            ExpressionAttributeValues={
                ":lease": _num(time.time() + LEASE_S),
                ":who": {"S": holder},
                ":ep": {"N": str(int(epoch))},
                ":pending": {"S": PENDING},
            },
        )

    def commit_effect(self, run_id, task_id, holder, epoch):
        """Commit the task's effect once, as the current holder, when due.

        The increment and the finished marker are one write, and the write
        only happens for the holder of the current epoch of a task that has
        never committed and is now due. Any other caller — a second copy of
        the delivery, a holder that lost the task while suspended — changes
        nothing.
        """
        now = time.time()
        ok = self._conditional(
            TableName=self.name,
            Key={"pk": {"S": task_key(run_id, task_id)}},
            UpdateExpression=(
                "ADD effect_count :one SET done_at = :now, phase = :done"
            ),
            ConditionExpression=(
                "phase = :pending AND holder = :who AND epoch = :ep AND"
                " (attribute_not_exists(effect_count) OR effect_count = :zero)"
                " AND due_at <= :now"
            ),
            ExpressionAttributeValues={
                ":one": {"N": "1"},
                ":now": _num(now),
                ":done": {"S": DONE},
                ":pending": {"S": PENDING},
                ":who": {"S": holder},
                ":ep": {"N": str(int(epoch))},
                ":zero": {"N": "0"},
            },
        )
        return now if ok else None

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
            "epoch": int(item.get("epoch", {"N": "0"})["N"]),
            "lease_until": (
                float(item["lease_until"]["N"]) if "lease_until" in item else None
            ),
            "effect_count": int(item.get("effect_count", {"N": "0"})["N"]),
            "done_at": (
                float(item["done_at"]["N"]) if "done_at" in item else None
            ),
        }
