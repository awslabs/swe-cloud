import json
import os
import time
import uuid
from retry import call
from botocore.exceptions import ClientError
from aws import client
from reservations import Members, Busy


class Conflict(Exception):
    def __init__(self, record):
        self.record = record


class RecordStore:
    def __init__(self):
        self.db = client()
        self.table = os.environ.get('IDEMPOTENCY_TABLE', 'idempotency')
        self.executions = os.environ.get('EXECUTION_TABLE', 'durable_executions')
        self.members_table = os.environ.get('DISPATCH_TABLE', 'dispatch_reservations')
        self.members = []

    def _write(self, key, ctx, action):
        try:
            request = dict(ClientRequestToken=str(uuid.uuid4()), TransactItems=[
                {'ConditionCheck': {
                    'TableName': self.executions,
                    'Key': {'execution_id': {'S': ctx.execution_id}},
                    'ConditionExpression': '#ticket = :ticket',
                    'ExpressionAttributeNames': {'#ticket': 'active_invocation_id'},
                    'ExpressionAttributeValues': {':ticket': {'S': ctx.invocation_id}}}},
                action])
            call(lambda: self.db.transact_write_items(**request), ctx)
        except ClientError as exc:
            reasons = exc.response.get('CancellationReasons', [])
            if (exc.response['Error']['Code'] != 'TransactionCanceledException'
                    or not any(r.get('Code') == 'ConditionalCheckFailed' for r in reasons)):
                raise
            row = call(lambda: self.db.get_item(TableName=self.table, Key={'id': {'S': key}},
                                               ConsistentRead=True), ctx).get('Item', {})
            raise Conflict(row) from exc

    def acquire(self, key, ctx, members=()):
        self.members = list(members)
        now = int(time.time() * 1000)
        deadline = ctx.execution_start_ms + ctx.execution_timeout_ms
        if now >= deadline:
            row = call(lambda: self.db.get_item(TableName=self.table, Key={'id': {'S': key}},
                                               ConsistentRead=True), ctx).get('Item', {})
            raise Conflict(row)
        item = {'id': {'S': key}, 'status': {'S': 'INPROGRESS'},
                'owner_execution_id': {'S': ctx.execution_id},
                'expiration': {'N': str(max(now // 1000 + 3600, (deadline + 999) // 1000 + 3600))},
                'in_progress_expiration': {'N': str(deadline)}}
        self._write(key, ctx, {'Put': {
            'TableName': self.table, 'Item': item,
            'ConditionExpression': 'attribute_not_exists(#id) OR '
                                   '(#status = :done AND #expiry <= :now) OR '
                                   '(#status = :running AND (#lease <= :ms OR #owner = :me))',
            'ExpressionAttributeNames': {'#id': 'id', '#expiry': 'expiration',
                                         '#status': 'status', '#lease': 'in_progress_expiration',
                                         '#owner': 'owner_execution_id'},
            'ExpressionAttributeValues': {':now': {'N': str(now // 1000)},
                                          ':ms': {'N': str(now)}, ':running': {'S': 'INPROGRESS'},
                                          ':done': {'S': 'COMPLETED'}, ':me': {'S': ctx.execution_id}}}})

        if self.members:
            try:
                Members(self).claim(key, ctx, self.members)
            except Busy as exc:
                row = call(lambda: self.db.get_item(TableName=self.table,
                    Key={'id': {'S': key}}, ConsistentRead=True), ctx).get('Item', {})
                raise Conflict(row) from exc

        active = call(lambda: self.db.get_item(TableName=self.executions,
                      Key={'execution_id': {'S': ctx.execution_id}}, ConsistentRead=True), ctx).get('Item', {})
        if active.get('active_invocation_id', {}).get('S') != ctx.invocation_id:
            row = call(lambda: self.db.get_item(TableName=self.table,
                       Key={'id': {'S': key}}, ConsistentRead=True), ctx).get('Item', {})
            raise Conflict(row)

    def complete(self, key, ctx, result):
        self._write(key, ctx, {'Update': {
            'TableName': self.table, 'Key': {'id': {'S': key}},
            'UpdateExpression': 'SET #s = :s, response_data = :data, expiration = :expiry',
            'ConditionExpression': '#owner = :me AND #s = :running',
            'ExpressionAttributeNames': {'#s': 'status', '#owner': 'owner_execution_id'},
            'ExpressionAttributeValues': {':s': {'S': 'COMPLETED'}, ':data': {'S': json.dumps(result)},
                                         ':me': {'S': ctx.execution_id}, ':running': {'S': 'INPROGRESS'},
                                         ':expiry': {'N': str(int(time.time()) + int(os.environ.get('RESPONSE_RETENTION_S', '3600')))}}}})

    def delete(self, key, ctx):
        try:
            Members(self).release(key, ctx, self.members)
            self._write(key, ctx, {'Delete': {
                'TableName': self.table, 'Key': {'id': {'S': key}},
                'ConditionExpression': '#owner = :me AND #s = :running',
                'ExpressionAttributeNames': {'#owner': 'owner_execution_id', '#s': 'status'},
                'ExpressionAttributeValues': {':me': {'S': ctx.execution_id},
                                             ':running': {'S': 'INPROGRESS'}}}})
        except Conflict:
            pass
