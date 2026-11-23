import json
import os
import time
import uuid
from decimal import Decimal
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
        self.generation = None

    def _write(self, key, ctx, action):
        try:
            request = dict(ClientRequestToken=str(uuid.uuid4()), TransactItems=[
                {'ConditionCheck': {
                    'TableName': self.executions,
                    'Key': {'execution_id': {'S': ctx.execution_id}},
                    'ConditionExpression': '#ticket = :ticket',
                    'ExpressionAttributeNames': {'#ticket': 'active_invocation_id'},
                    'ExpressionAttributeValues': {':ticket': {'S': ctx.invocation_id}}}},
                *(action if isinstance(action, list) else [action])])
            call(lambda: self.db.transact_write_items(**request), ctx)
        except ClientError as exc:
            reasons = exc.response.get('CancellationReasons', [])
            if (exc.response['Error']['Code'] != 'TransactionCanceledException'
                    or not any(r.get('Code') == 'ConditionalCheckFailed' for r in reasons)):
                raise
            row = call(lambda: self.db.get_item(TableName=self.table, Key={'id': {'S': key}},
                                               ConsistentRead=True), ctx).get('Item', {})
            raise Conflict(row) from exc

    def _parent_check(self, key, generation):
        return {'ConditionCheck': {
            'TableName': self.table, 'Key': {'id': {'S': key}},
            'ConditionExpression': '#g = :g AND #s = :running AND released = :no',
            'ExpressionAttributeNames': {'#g': 'reservation_id', '#s': 'status'},
            'ExpressionAttributeValues': {':g': {'S': generation},
                ':running': {'S': 'INPROGRESS'}, ':no': {'BOOL': False}}}}

    def _read(self, key, ctx):
        return call(lambda: self.db.get_item(TableName=self.table,
                    Key={'id': {'S': key}}, ConsistentRead=True), ctx).get('Item', {})

    def acquire(self, key, ctx, members=()):
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
        members = sorted(set(members))
        previous = self._read(key, ctx) if members else {}
        if (members and previous.get('owner_execution_id', {}).get('S') == ctx.execution_id
                and previous.get('status', {}).get('S') == 'INPROGRESS'
                and not previous.get('released', {}).get('BOOL')
                and Decimal(previous['in_progress_expiration']['N']) > now):
            self.generation = previous['reservation_id']['S']
            self._write(key, ctx, self._parent_check(key, self.generation))
        else:
            if members:
                self.generation = uuid.uuid4().hex
                item.update(reservation_id={'S': self.generation}, released={'BOOL': False},
                            dispatch_keys={'SS': members})
            condition = ('attribute_not_exists(#id) OR (#status = :done AND #expiry <= :now) OR '
                         '(#status = :running AND #lease <= :ms)')
            values = {':now': {'N': str(now // 1000)}, ':ms': {'N': str(now)},
                      ':running': {'S': 'INPROGRESS'}, ':done': {'S': 'COMPLETED'}}
            if not members:
                condition += ' OR (#status = :running AND #owner = :me)'
                values[':me'] = {'S': ctx.execution_id}
            self._write(key, ctx, {'Put': {
                'TableName': self.table, 'Item': item,
                'ConditionExpression': condition,
                'ExpressionAttributeNames': {'#id': 'id', '#expiry': 'expiration',
                                             '#status': 'status', '#lease': 'in_progress_expiration',
                                             **({'#owner': 'owner_execution_id'} if not members else {})},
                'ExpressionAttributeValues': values}})

        if members:
            try:
                Members(self).claim(key, ctx, members, self.generation)
            except (Busy, Conflict) as exc:
                self.delete(key, ctx)
                raise Conflict(self._read(key, ctx)) from exc

        if members:
            current = self._read(key, ctx)
            if (int(time.time() * 1000) >= deadline
                    or current.get('reservation_id', {}).get('S') != self.generation
                    or current.get('released', {}).get('BOOL')
                    or current.get('status', {}).get('S') != 'INPROGRESS'):
                raise Conflict(current)

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
            'ConditionExpression': '#owner = :me AND #s = :running' +
                                   (' AND reservation_id = :g AND released = :no' if self.generation else ''),
            'ExpressionAttributeNames': {'#s': 'status', '#owner': 'owner_execution_id'},
            'ExpressionAttributeValues': {':s': {'S': 'COMPLETED'}, ':data': {'S': json.dumps(result)},
                                         ':me': {'S': ctx.execution_id}, ':running': {'S': 'INPROGRESS'},
                                         ':expiry': {'N': str(int(time.time()) + int(os.environ.get('RESPONSE_RETENTION_S', '3600')))},
                                         **({':g': {'S': self.generation}, ':no': {'BOOL': False}}
                                            if self.generation else {})}}})

    def delete(self, key, ctx):
        if self.generation:
            try:
                self._write(key, ctx, {'Update': {
                    'TableName': self.table, 'Key': {'id': {'S': key}},
                    'UpdateExpression': 'SET released = :yes, in_progress_expiration = :zero',
                    'ConditionExpression': '#g = :g AND #s = :running',
                    'ExpressionAttributeNames': {'#g': 'reservation_id', '#s': 'status'},
                    'ExpressionAttributeValues': {':g': {'S': self.generation},
                        ':running': {'S': 'INPROGRESS'}, ':yes': {'BOOL': True}, ':zero': {'N': '0'}}}})
            except Conflict:
                pass
            return
        try:
            self._write(key, ctx, {'Delete': {
                'TableName': self.table, 'Key': {'id': {'S': key}},
                'ConditionExpression': '#owner = :me AND #s = :running',
                'ExpressionAttributeNames': {'#owner': 'owner_execution_id', '#s': 'status'},
                'ExpressionAttributeValues': {':me': {'S': ctx.execution_id},
                                             ':running': {'S': 'INPROGRESS'}}}})
        except Conflict:
            pass
