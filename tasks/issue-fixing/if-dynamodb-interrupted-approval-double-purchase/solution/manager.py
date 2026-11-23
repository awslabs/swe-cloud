import os
import uuid

from gateway import Gateway, token_for
from hooks import seam
from storage import Store, lost_race, now_ms


class OwnershipLost(Exception):
    pass


class AuditLoss(Exception):
    pass


def owned_update(store, row, owner_field, owner, update, values, names=None):
    names = {**(names or {}), '#s': 'status', '#owner': owner_field}
    expected = 'approved' if owner_field == 'approve_run' else 'running'
    try:
        return store.change(row['execution_id'], update,
                            {**values, ':expected': expected, ':owner': owner},
                            '#s=:expected AND #owner=:owner', names)
    except Exception as exc:
        if lost_race(exc):
            raise OwnershipLost(row['execution_id']) from exc
        raise AuditLoss(f"Unable to persist purchase history for {row['execution_id']}: {exc}") from exc


def checkpoint(store, row, owner_field, owner):
    owned_update(store, row, owner_field, owner, 'SET recommendations=:recs',
                 {':recs': row['recommendations']})


def execute(row, store, owner_field, owner, recovering):
    gateway = Gateway()
    errors = []
    selected = [rec for rec in row['recommendations'] if rec.get('selected', True)]
    bought = 0
    deferred = False
    for rec in selected:
        if rec.get('commitment_id'):
            bought += 1
            continue
        try:
            token = token_for(rec.get('purchase_execution_id', row['execution_id']), rec['index'])
            found = [] if rec['provider_type'] == 'none' else gateway.describe(rec['provider_type'], token)
            found = [item for item in found if item['offering_id'] == rec['offering_id']]
            if len(found) > 1:
                raise RuntimeError('Multiple provider commitments require operator review')
            if found:
                commitment_id = found[0]['commitment_id']
            else:
                # These adapters cannot establish absence after an interrupted request.
                uncertain = (recovering or bool(row.get('retry_of'))) and rec.get('intent_state') != 'ready'
                if uncertain and rec['provider_type'] != 'token':
                    raise RuntimeError('Outcome unknown; verify provider account before any new purchase')
                if rec.get('attempted_at') and rec['provider_type'] != 'token':
                    raise RuntimeError('Prior purchase outcome unknown; operator review required')
                if uncertain and rec['provider_type'] == 'token' and now_ms() < rec.get('safe_after', 0):
                    deferred = True
                    continue
                rec['attempted_at'] = now_ms()
                rec['intent_state'] = 'attempted'
                rec['safe_after'] = now_ms() + 20000
                checkpoint(store, row, owner_field, owner)
                commitment_id = gateway.purchase(rec, token)
                # Persist the returned reference before a separate tagging request.
                rec['commitment_id'] = commitment_id
                rec['purchased'] = True
                checkpoint(store, row, owner_field, owner)
                if rec['provider_type'] == 'tag_after':
                    gateway.tag(commitment_id, token)
            rec['commitment_id'] = commitment_id
            rec['purchased'] = True
            rec.pop('error', None)
            checkpoint(store, row, owner_field, owner)
            bought += 1
        except (OwnershipLost, AuditLoss):
            raise
        except Exception as exc:
            rec['error'] = str(exc)
            errors.append(f"recommendation {rec['index']}: {exc}")
            if rec.get('commitment_id'):
                bought += 1
    if deferred:
        checkpoint(store, row, owner_field, owner)
        return
    status = 'completed' if bought == len(selected) else ('partially_completed' if bought else 'failed')
    owned_update(store, row, owner_field, owner,
                 'SET #s=:terminal, recommendations=:recs, #error=:error, completed_at=:now, updated_at=:now',
                 {':terminal': status, ':recs': row['recommendations'], ':error': '; '.join(errors), ':now': now_ms()},
                 {'#error': 'error'})


def approve(execution_id, actor):
    store = Store()
    owner = uuid.uuid4().hex
    initial = store.get(execution_id)
    if initial is None or initial['status'] not in ('pending', 'notified'):
        raise ValueError('Only pending or notified executions can be approved')
    recs = initial['recommendations']
    if not initial.get('retry_of'):
        for rec in recs:
            rec.setdefault('intent_state', 'ready')
    row = store.change(execution_id,
                       'SET #s=:approved, updated_at=:now, approved_by=:actor, approve_run=:owner, recommendations=:recs',
                       {':approved': 'approved', ':pending': 'pending', ':notified': 'notified',
                        ':now': now_ms(), ':actor': actor, ':owner': owner, ':recs': recs},
                       '#s IN (:pending,:notified)', {'#s': 'status'})
    execute(row, store, 'approve_run', owner, False)


def sweep():
    store = Store()
    stale_ms = int(float(os.environ.get('STALE_APPROVED_THRESHOLD_S', '30')) * 1000)
    lease_ms = int(float(os.environ.get('CLAIM_LEASE_S', '30')) * 1000)
    recovered = 0
    errors = []
    for candidate in store.scan():
        now = now_ms()
        stale = candidate['status'] == 'approved' and candidate['updated_at'] < now - stale_ms
        expired = candidate['status'] == 'running' and candidate.get('claim_expires_at', now + 1) < now
        if not (stale or expired):
            continue
        seam('stage_2', execution_id=candidate['execution_id'], status=candidate['status'], updated_at=int(candidate['updated_at']))
        owner = uuid.uuid4().hex
        try:
            row = store.change(candidate['execution_id'],
                               'SET #s=:running, claim_owner=:owner, claim_expires_at=:expires, updated_at=:now',
                               {':approved': 'approved', ':running': 'running', ':cutoff': now - stale_ms,
                                ':now': now, ':owner': owner, ':expires': now + lease_ms},
                               '(#s=:approved AND updated_at<:cutoff) OR (#s=:running AND claim_expires_at<:now)',
                               {'#s': 'status'})
        except Exception as exc:
            if not lost_race(exc):
                errors.append(str(exc))
            continue
        try:
            execute(row, store, 'claim_owner', owner, True)
            recovered += 1
        except OwnershipLost:
            continue
        except Exception as exc:
            errors.append(str(exc))
    if errors:
        raise AuditLoss('; '.join(errors))
    return {'recovered': recovered}
