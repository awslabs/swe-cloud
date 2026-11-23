"""Durable, single-successor acceptance for operator Retry requests."""
import copy
import time
import uuid

from botocore.exceptions import ClientError
from hooks import seam
from storage import Store, now_ms


def retry(execution_id):
    store = Store()
    deadline = time.monotonic() + 4
    while True:
        parent = store.get(execution_id)
        if not parent or parent['status'] not in ('failed', 'partially_completed'):
            raise ValueError('Only failed or partially completed executions can be retried')
        if parent.get('retry_execution_id'):
            return parent['retry_execution_id']
        recs = copy.deepcopy(parent['recommendations'])
        for rec in recs:
            rec.setdefault('purchase_execution_id', parent['execution_id'])
            # Old terminal rows predate intent checkpoints. They are not proof
            # that a selected recommendation was never dispatched.
            rec.setdefault('intent_state', 'attempted')
            rec['safe_after'] = max(int(rec.get('safe_after', 0)),
                                    int(parent['updated_at']) + 20000)
            rec.pop('error', None)
        child = dict(execution_id=str(uuid.uuid4()), retry_of=execution_id,
                     status='pending', updated_at=now_ms(), recommendations=recs)
        try:
            store.db.meta.client.transact_write_items(TransactItems=[
                {'Update': dict(TableName=store.table.name, Key={'execution_id': execution_id},
                                UpdateExpression='SET retry_execution_id=:child',
                                ConditionExpression='#s IN (:failed,:partial) AND attribute_not_exists(retry_execution_id)',
                                ExpressionAttributeNames={'#s': 'status'},
                                ExpressionAttributeValues={':child': child['execution_id'], ':failed': 'failed',
                                                           ':partial': 'partially_completed'})},
                {'Put': dict(TableName=store.table.name, Item=child,
                             ConditionExpression='attribute_not_exists(execution_id)')}
            ])
            seam('stage_3', execution_id=execution_id, child_id=child['execution_id'])
            return child['execution_id']
        except ClientError as exc:
            if exc.response['Error']['Code'] not in ('TransactionCanceledException', 'TransactionConflictException'):
                raise
            # A rival may have accepted the child while this request was in
            # flight. Re-read to return its durable answer. Other transaction
            # failures are surfaced rather than spinning on an unavailable DB.
            current = store.get(execution_id)
            if current and current.get('retry_execution_id'):
                return current['retry_execution_id']
            reasons = exc.response.get('CancellationReasons', [])
            if time.monotonic() >= deadline or any(reason.get('Code') not in
                    ('None', 'ConditionalCheckFailed', 'TransactionConflict') for reason in reasons):
                raise
            time.sleep(0.05)
