import os
from gateway import Gateway, token_for
from hooks import seam
from storage import Store, lost_race, now_ms


def execute(row, store):
    gateway = Gateway()
    errors = []
    bought = 0
    selected = [rec for rec in row['recommendations'] if rec.get('selected', True)]
    for rec in selected:
        try:
            if not rec.get('commitment_id'):
                rec['commitment_id'] = gateway.buy(rec, token_for(row['execution_id'], rec['index']))
                rec['purchased'] = True
            bought += 1
        except Exception as exc:
            errors.append(f"recommendation {rec['index']}: {exc}")
    row['status'] = 'completed' if bought == len(selected) else ('partially_completed' if bought else 'failed')
    row['error'] = '; '.join(errors)
    row['completed_at'] = now_ms()
    row['updated_at'] = now_ms()
    store.save(row)


def approve(execution_id, actor):
    store = Store()
    row = store.change(execution_id,
                       'SET #s=:a, updated_at=:now, approved_by=:actor',
                       {':a': 'approved', ':p': 'pending', ':n': 'notified', ':now': now_ms(), ':actor': actor},
                       '#s IN (:p,:n)', {'#s': 'status'})
    execute(row, store)


def sweep():
    store = Store()
    cutoff = now_ms() - int(float(os.environ.get('STALE_APPROVED_THRESHOLD_S', '30')) * 1000)
    recovered = 0
    for row in store.scan():
        if row['status'] != 'approved' or row['updated_at'] >= cutoff:
            continue
        seam('stage_2', execution_id=row['execution_id'], status=row['status'], updated_at=int(row['updated_at']))
        try:
            store.change(row['execution_id'], 'SET #s=:failed, #e=:error, updated_at=:now, completed_at=:now',
                         {':failed': 'failed', ':approved': 'approved', ':error': 'Purchase worker did not finish. Verify the cloud account before retrying.', ':now': now_ms()},
                         '#s=:approved', {'#s': 'status', '#e': 'error'})
            recovered += 1
        except Exception as exc:
            if not lost_race(exc):
                raise
    return {'recovered': recovered}
