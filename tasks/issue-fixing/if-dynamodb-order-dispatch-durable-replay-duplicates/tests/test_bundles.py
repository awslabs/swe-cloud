"""Bundle outcomes through the public decorator, independent contexts and AWS rows."""
import json
import os
import select
import time
import uuid
import pytest
from test_dispatch import Deployment, LOG_ROOT, digest
from test_replacement import Worker
from transport import Link
from uncertain_transport import UncertainLink


def event(names):
    return {'order': uuid.uuid4().hex, 'warehouse': 'bundles', 'dispatch_keys': list(names)}


def identities(count):
    prefix = uuid.uuid4().hex
    return [prefix + ':' + str(i).zfill(4) for i in range(count)]


def request(d, e, **kwargs):
    r = d.request(e, wait_s=0, **kwargs)
    r.update(invocation_timeout_s=20, audit_callback=True,
             direct_result={'receipt': uuid.uuid4().hex, 'bundle': e['order']})
    return r


def callbacks(d, r):
    row = d.db.get_item(TableName=d.tables['EFFECTS_TABLE'],
        Key={'payload_key': {'S': digest(r['event'])}, 'execution_id': {'S': r['execution_id']}},
        ConsistentRead=True).get('Item', {})
    return int(row.get('callback_count', {}).get('N', '0'))


def success(output, r):
    assert output.get('status') == 'SUCCEEDED', output
    assert output['result'] == {**r['direct_result'], 'callback_number': 1}, output


def conflict(d, r):
    output = d.invoke(r)
    assert output.get('error') == 'AlreadyInProgress', output
    assert callbacks(d, r) == 0, (r, output)
    return output


def remember(name, evidence):
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    (LOG_ROOT / ('bundle-' + name + '.json')).write_text(json.dumps(evidence, indent=2))


def intercepted_or_finished(link, worker):
    # Await either a write in this representation or a completed admission, not
    # a fixed sleep followed by an assumption about where an alternative stores locks.
    end = time.monotonic() + 24
    while time.monotonic() < end:
        if link.pending.is_set():
            return True
        if worker.process.poll() is not None:
            return False
        ready, _, _ = select.select([worker.listener], [], [], 0.02)
        if ready:
            worker.accept_handler()
            worker.resume()
            return False
    raise AssertionError('bundle admission exceeded its invocation budget')


def test_large_bundle_replays_and_disjoint_orders_progress():
    d = Deployment()
    names = identities(257)
    owner = request(d, event(names + names[:3]))
    owner.pop('direct_result')
    first = d.invoke(owner)
    assert first.get('status') == 'SUSPENDED', first
    rival = request(d, event([names[128]]))
    conflict(d, rival)
    disjoint = request(d, event(identities(129)))
    success(d.invoke(disjoint), disjoint)
    second = d.invoke(owner)
    assert second.get('status') == 'SUCCEEDED', second
    cache = d.invoke(d.request(owner['event'], wait_s=0))
    assert cache.get('result') == second['result'], (second, cache)
    assert d.effects(owner['event']) == [owner['execution_id']]
    remember('large-replay', {'first': first, 'second': second, 'cache': cache})


def test_refused_partial_bundle_does_not_strand_unrelated_identities():
    d = Deployment()
    names = identities(257)
    blocker = request(d, event([names[-1]]))
    success(d.invoke(blocker), blocker)
    rejected = request(d, event(names))
    refused = conflict(d, rejected)
    # The overlap may be encountered after multiple committed batches. No callback
    # ran for this admission, so its earlier nonoverlapping claims must be usable.
    time.sleep(3.1)  # The documented grace is for availability, not callback duration.
    probe = request(d, event(names[:-1]))
    output = d.invoke(probe)
    success(output, probe)
    assert d.item(blocker['event'])['owner_execution_id']['S'] == blocker['execution_id']
    conflict(d, request(d, event([names[-1]])))
    remember('partial-conflict', {'refused': refused, 'probe': output})


def test_handler_failure_releases_entire_large_bundle():
    d = Deployment()
    names = identities(205)
    owner = request(d, event(names))
    owner.pop('direct_result')
    owner['handler_error'] = True
    output = d.invoke(owner)
    assert output.get('error') == 'ValueError', output
    assert callbacks(d, owner) == 1
    time.sleep(3.1)
    new = request(d, event(list(reversed(names))))
    success(d.invoke(new), new)


def test_killed_owner_keeps_shared_reservations_and_can_retry():
    d = Deployment()
    names = identities(203)
    owner = request(d, event(names))
    output = d.invoke(owner, kill=True)
    assert output.get('status') == 'KILLED', output
    conflict(d, request(d, event([names[150]])))
    success(d.invoke(owner), owner)
    assert callbacks(d, owner) == 1


def test_completed_bundle_uses_response_horizon_not_execution_deadline(monkeypatch):
    monkeypatch.setenv('RESPONSE_RETENTION_S', '5')
    d = Deployment()
    names = identities(3)
    owner = request(d, event(names), timeout_ms=2000)
    success(d.invoke(owner), owner)
    horizon = float(d.item(owner['event'])['expiration']['N'])
    until = owner['execution_start_ms'] + owner['execution_timeout_ms']
    time.sleep(max(0, until / 1000 - time.time() + 0.1))
    conflict(d, request(d, event([names[0]])))
    cached = d.invoke(request(d, owner['event']))
    assert cached.get('result') == {**owner['direct_result'], 'callback_number': 1}, cached
    time.sleep(max(0, horizon - time.time() + 0.1))
    new = request(d, event([names[0], *identities(102)]))
    output = d.invoke(new)
    success(output, new)
    remember('completed-horizon', {'new': output, 'cache': cached, 'horizon': horizon})


def test_abandoned_partial_or_full_bundle_reclaims_at_absolute_deadline():
    d = Deployment()
    names = identities(207)
    owner = request(d, event(names), timeout_ms=2200)
    # Killed after admission, before any callback work/checkpoint.
    assert d.invoke(owner, kill=True).get('status') == 'KILLED'
    deadline = (owner['execution_start_ms'] + owner['execution_timeout_ms']) / 1000
    time.sleep(max(0, deadline - time.time() + 3.1))
    new = request(d, event(names))
    output = d.invoke(new)
    success(output, new)


def test_reclaimed_members_cannot_be_revived_by_delayed_parent_completion():
    d = Deployment()
    names = identities(3)
    old = d.issue(request(d, event(names), timeout_ms=2200))
    with Link(os.environ['AWS_ENDPOINT_URL'], d.tables['IDEMPOTENCY_TABLE'], hold_all=True) as link:
        worker = Worker(d, old, link.endpoint)
        try:
            worker.accept_handler()
            link.armed.set()
            worker.resume()
            assert link.pending.wait(5), 'no terminal order mutation'
            before = d.item(old['event'])
            deadline = (old['execution_start_ms'] + old['execution_timeout_ms']) / 1000
            time.sleep(max(0, deadline - time.time() + 3.1))
            new = request(d, event(names))
            new_output = d.invoke(new)
            # An alternate implementation may already have published before its
            # held bookkeeping write. That publication still protects the bundle.
            earlier_publication = before.get('status', {}).get('S') == 'COMPLETED'
            if earlier_publication:
                assert new_output.get('error') == 'AlreadyInProgress', new_output
                assert callbacks(d, new) == 0
            else:
                success(new_output, new)
            link.release.set()
            assert link.applied.wait(5)
            arrival = d.item(old['event'])
            link.deliver.set()
            old_output = worker.result()
            if not earlier_publication:
                assert arrival.get('status', {}).get('S') != 'COMPLETED', arrival
                assert old_output.get('error') == 'AlreadyInProgress', old_output
                success(d.invoke(request(d, new['event'])), new)
            else:
                assert old_output.get('status') == 'SUCCEEDED', old_output
            remember('late-completion', {'before': before, 'arrival': arrival,
                     'old': old_output, 'new': new_output})
        finally:
            worker.close()


def test_interrupted_multiwrite_admission_continues_without_losing_claims():
    d = Deployment()
    names = identities(259)
    old = d.issue(request(d, event(names)))
    with UncertainLink(os.environ['AWS_ENDPOINT_URL'], d.tables['DISPATCH_TABLE'], True) as link:
        link.armed.set()
        worker = Worker(d, old, link.endpoint)
        try:
            if intercepted_or_finished(link, worker):
                link.release.set()
                assert link.applied.wait(5)
                worker.process.kill()
                worker.process.communicate(timeout=2)
                link.deliver.set()
            else:
                # A correct implementation can use another representation or
                # commit the entire plan in one aggregate item. Do not pin it to
                # the supplied member-table layout or require partial progress.
                success(worker.result(), old)
        finally:
            worker.close()
    continuation = d.invoke(old)
    success(continuation, old)
    assert callbacks(d, old) == 1
    conflict(d, request(d, event([names[0], names[-1]])))


@pytest.mark.parametrize('committed', [False, True])
def test_uncertain_member_admission_respects_replacement(committed):
    d = Deployment()
    names = identities(201)
    old = d.issue(request(d, event(names)))
    with UncertainLink(os.environ['AWS_ENDPOINT_URL'], d.tables['DISPATCH_TABLE'], committed) as link:
        link.armed.set()
        worker = Worker(d, old, link.endpoint)
        try:
            intercepted = intercepted_or_finished(link, worker)
            if intercepted:
                link.release.set()
                assert link.applied.wait(5)
                successor = {**old, 'direct_result': {'receipt': uuid.uuid4().hex, 'worker': 'current'}}
                replacement = d.invoke(successor)
                success(replacement, successor)
                link.deliver.set()
                result = worker.result()
                assert not worker.entered, result
                if result.get('status') == 'SUCCEEDED':
                    assert result['result'] == replacement['result'], (result, replacement)
                else:
                    assert result.get('error') == 'AlreadyInProgress', result
                assert callbacks(d, old) == 1, result
            else:
                success(worker.result(), old)
            conflict(d, request(d, event([names[100]])))
        finally:
            worker.close()


def test_uncertain_member_acknowledgement_does_not_extend_execution_lifetime():
    d = Deployment()
    names = identities(3)
    old = d.issue(request(d, event(names), timeout_ms=2200))
    with UncertainLink(os.environ['AWS_ENDPOINT_URL'], d.tables['DISPATCH_TABLE'], True) as link:
        link.armed.set()
        worker = Worker(d, old, link.endpoint)
        try:
            if intercepted_or_finished(link, worker):
                link.release.set()
                assert link.applied.wait(5)
                deadline = (old['execution_start_ms'] + old['execution_timeout_ms']) / 1000
                time.sleep(max(0, deadline - time.time() + 3.1))
                new = request(d, event(names))
                output = d.invoke(new)
                success(output, new)
                link.deliver.set()
                retired = worker.result()
                assert not worker.entered, retired
                assert callbacks(d, old) == 0, retired
                assert retired.get('error') == 'AlreadyInProgress', retired
                success(d.invoke(request(d, new['event'])), new)
                remember('aged-admission', {'old': retired, 'new': output})
            else:
                # A completed alternative plan is protected by retention instead.
                success(worker.result(), old)
                conflict(d, request(d, event(names)))
        finally:
            worker.close()
