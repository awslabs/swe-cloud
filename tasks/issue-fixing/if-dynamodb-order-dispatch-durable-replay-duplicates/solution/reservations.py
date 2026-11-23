"""Shared dispatch identities refer to a fenced generation of an order record."""
import time
from decimal import Decimal
from botocore.exceptions import ClientError
from retry import call


class Busy(Exception):
    pass


class Members:
    def __init__(self, store):
        self.store = store

    def _get(self, table, key, ctx):
        return call(lambda: self.store.db.get_item(TableName=table,
                    Key={'id': {'S': key}}, ConsistentRead=True), ctx).get('Item', {})

    def _retire(self, pointer, ctx):
        """Detach only after the prior parent cannot commit or extend this generation."""
        store = self.store
        parent_id = pointer['parent_id']['S']
        generation = pointer['generation']['S']
        row = self._get(store.table, parent_id, ctx)
        if not row or row.get('reservation_id', {}).get('S') != generation:
            return
        if row.get('released', {}).get('BOOL'):
            return
        now = int(time.time() * 1000)
        if row['status']['S'] == 'COMPLETED':
            if Decimal(row['expiration']['N']) > now // 1000:
                raise Busy(parent_id)
            predicate = '#s = :done AND #expiry <= :seconds'
            names = {'#s': 'status', '#expiry': 'expiration', '#g': 'reservation_id'}
            values = {':done': {'S': 'COMPLETED'}, ':seconds': {'N': str(now // 1000)}}
        else:
            if Decimal(row['in_progress_expiration']['N']) > now:
                raise Busy(parent_id)
            predicate = '#s = :running AND #lease <= :now'
            names = {'#s': 'status', '#lease': 'in_progress_expiration', '#g': 'reservation_id'}
            values = {':running': {'S': 'INPROGRESS'}, ':now': {'N': str(now)}}
        # Reclamation has no authority over a live parent. This conditional update
        # fences its terminal writes before any member is reassigned.
        values.update({':g': {'S': generation}, ':yes': {'BOOL': True}, ':zero': {'N': '0'}})
        try:
            call(lambda: store.db.update_item(TableName=store.table,
                Key={'id': {'S': parent_id}},
                UpdateExpression='SET released = :yes, in_progress_expiration = :zero',
                ConditionExpression='#g = :g AND (' + predicate + ')',
                ExpressionAttributeNames=names, ExpressionAttributeValues=values), ctx)
        except ClientError as exc:
            if exc.response['Error']['Code'] != 'ConditionalCheckFailedException':
                raise
            # A completion/continuation won the race. Re-evaluate its actual lifetime.
            return self._retire(pointer, ctx)

    def claim(self, key, ctx, names, generation):
        store = self.store
        for offset in range(0, len(names), 98):
            actions = []
            for name in names[offset:offset + 98]:
                previous = self._get(store.members_table, name, ctx)
                if previous and previous.get('generation', {}).get('S') != generation:
                    self._retire(previous, ctx)
                action = {'TableName': store.members_table,
                          'Item': {'id': {'S': name}, 'parent_id': {'S': key},
                                   'generation': {'S': generation}}}
                if previous:
                    action.update(ConditionExpression='#g = :old',
                                  ExpressionAttributeNames={'#g': 'generation'},
                                  ExpressionAttributeValues={':old': previous['generation']})
                else:
                    action.update(ConditionExpression='attribute_not_exists(id)')
                actions.append({'Put': action})
            store._write(key, ctx, [store._parent_check(key, generation), *actions])
