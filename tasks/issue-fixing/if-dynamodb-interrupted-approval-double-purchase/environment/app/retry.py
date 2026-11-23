"""Operator Retry: create a reviewable successor to an unsuccessful execution."""
import copy
import uuid

from hooks import seam
from storage import Store, now_ms


def retry(execution_id):
    store = Store()
    parent = store.get(execution_id)
    if not parent or parent['status'] not in ('failed', 'partially_completed'):
        raise ValueError('Only failed or partially completed executions can be retried')
    recommendations = copy.deepcopy(parent['recommendations'])
    for rec in recommendations:
        for key in ('commitment_id', 'purchased', 'error', 'attempted_at'):
            rec.pop(key, None)
    child = dict(execution_id=str(uuid.uuid4()), retry_of=execution_id,
                 status='pending', updated_at=now_ms(), recommendations=recommendations)
    store.save(child)
    seam('stage_3', execution_id=execution_id, child_id=child['execution_id'])
    return child['execution_id']
