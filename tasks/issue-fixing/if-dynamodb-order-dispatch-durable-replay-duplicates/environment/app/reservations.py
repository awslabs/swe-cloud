"""Shared dispatch-identity leases for bundle submissions."""
import time
from botocore.exceptions import ClientError


class Busy(Exception):
    pass


class Members:
    def __init__(self, store):
        self.store = store

    def claim(self, key, ctx, names):
        for name in sorted(set(names)):
            try:
                self.store._write(key, ctx, {'Put': {
                    'TableName': self.store.members_table,
                    'Item': {'id': {'S': name}, 'parent_id': {'S': key},
                             'owner_execution_id': {'S': ctx.execution_id},
                             'lease': {'N': str(ctx.execution_start_ms + ctx.execution_timeout_ms)}},
                    'ConditionExpression': 'attribute_not_exists(id) OR lease <= :now OR '
                                           '(parent_id = :p AND owner_execution_id = :e)',
                    'ExpressionAttributeValues': {':now': {'N': str(int(time.time() * 1000))},
                        ':p': {'S': key}, ':e': {'S': ctx.execution_id}}}})
            except Exception as exc:
                # The order reservation is still useful to its execution's retry.
                raise Busy(name) from exc

    def release(self, key, ctx, names):
        for name in sorted(set(names)):
            self.store._write(key, ctx, {'Delete': {
                'TableName': self.store.members_table, 'Key': {'id': {'S': name}},
                'ConditionExpression': 'parent_id = :p AND owner_execution_id = :e',
                'ExpressionAttributeValues': {':p': {'S': key}, ':e': {'S': ctx.execution_id}}}})
