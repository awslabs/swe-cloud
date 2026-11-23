import json
from decimal import Decimal
import os
import time
import uuid
import pytest
from test_dispatch import Deployment, LOG_ROOT, digest
from test_replacement import Worker
from uncertain_transport import UncertainLink


CASES = [(phase, commit, 'none') for phase in ('acquire', 'complete', 'delete') for commit in (False, True)]
CASES += [('acquire', True, 'gap'), ('complete', False, 'gap'), ('complete', True, 'gap'),
          ('complete', False, 'successor'), ('delete', False, 'gap'), ('delete', True, 'gap'),
          ('delete', False, 'successor'), ('delete', True, 'successor')]


@pytest.fixture(scope='module', params=CASES)
def uncertainty(request):
    phase, commit, rotation = request.param
    deployment = Deployment()
    event = {'order': uuid.uuid4().hex, 'warehouse': 'coast'}
    original = deployment.request(event, timeout_ms=90000, wait_s=0)
    original['audit_callback'] = True
    if phase == 'delete':
        original['handler_error'] = True
    else:
        original['direct_result'] = {'receipt': uuid.uuid4().hex, 'source': 'original'}
    original = deployment.issue(original)
    evidence = {'phase': phase, 'commit': commit, 'rotation': rotation, 'request': original}
    with UncertainLink(os.environ.get('AWS_ENDPOINT_URL', 'http://localhost:4566'),
                       deployment.tables['IDEMPOTENCY_TABLE'], commit) as link:
        if phase == 'acquire':
            link.armed.set()
        worker = Worker(deployment, original, link.endpoint)
        try:
            if phase != 'acquire':
                worker.accept_handler()
                link.armed.set()
                worker.resume()
            assert link.pending.wait(8), 'no order mutation reached gateway'
            evidence['before'] = deployment.item(event)
            link.release.set()
            assert link.applied.wait(8), 'first request did not reach an outcome'
            evidence['first'] = deployment.item(event)
            successor = {**original}
            successor.pop('handler_error', None)
            successor.pop('audit_callback', None)
            successor['direct_result'] = {'receipt': uuid.uuid4().hex, 'source': 'successor'}
            if rotation != 'none':
                successor = deployment.issue(successor)
                if rotation == 'successor':
                    evidence['successor_early'] = deployment.invoke(successor)
            evidence['at_response'] = deployment.item(event)
            registry = deployment.db.get_item(TableName=deployment.tables['EXECUTION_TABLE'],
                Key={'execution_id': {'S': original['execution_id']}}, ConsistentRead=True)['Item']
            link.deliver.set()
            evidence['result'] = worker.result()
            evidence['entered'] = worker.entered
            evidence['after'] = deployment.item(event)
            evidence['registry_unchanged'] = registry == deployment.db.get_item(
                TableName=deployment.tables['EXECUTION_TABLE'],
                Key={'execution_id': {'S': original['execution_id']}}, ConsistentRead=True)['Item']
            evidence['callbacks'] = deployment.db.get_item(TableName=deployment.tables['EFFECTS_TABLE'],
                Key={'payload_key': {'S': digest(event)}, 'execution_id': {'S': original['execution_id']}},
                ConsistentRead=True).get('Item', {}).get('callback_count', {}).get('N', '0')
            # Probe progress on an isolated healthy connection after recovery.
            if phase == 'delete' and rotation == 'none':
                evidence['next'] = deployment.invoke(deployment.request(event, wait_s=0) | {
                    'direct_result': successor['direct_result']})
            else:
                evidence['next'] = deployment.invoke(successor)
            evidence['next_item'] = deployment.item(event)
            evidence['wire'] = link.trace
            evidence['link_errors'] = link.errors[:]
        finally:
            worker.close()
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    (LOG_ROOT / ('uncertainty-' + phase + '-' + str(commit) + '-' + rotation + '.json')).write_text(
        json.dumps(evidence, indent=2))
    return evidence


def test_database_recovery_does_not_reenter_callback(uncertainty):
    e = uncertainty
    expected = 0 if e['phase'] == 'acquire' and e['rotation'] != 'none' else 1
    assert int(e['callbacks']) == expected, e
    assert e['registry_unchanged'], e
    assert not e['link_errors'], e


def test_uncertain_response_is_resolved(uncertainty):
    e = uncertainty
    output = e['result']
    if e['phase'] == 'delete':
        assert output.get('error') == 'ValueError', e
    elif e['rotation'] == 'none' or (e['phase'] == 'complete' and
            e['first'].get('status', {}).get('S') == 'COMPLETED'):
        assert output.get('status') == 'SUCCEEDED', e
        assert output.get('result') == {**e['request']['direct_result'], 'callback_number': 1}, e
    elif output.get('status') == 'SUCCEEDED':
        committed = e['at_response']
        assert committed.get('status', {}).get('S') == 'COMPLETED', e
        assert output.get('result') == json.loads(committed['response_data']['S']), e
    else:
        assert output.get('error') == 'AlreadyInProgress', e


def test_recovery_preserves_publication_and_successor_progress(uncertainty):
    e = uncertainty
    if e['rotation'] != 'none':
        expected = e['at_response']
        if expected:
            for name in ('status', 'owner_execution_id'):
                assert e['after'].get(name) == expected.get(name), e
            if expected['status']['S'] == 'COMPLETED':
                assert json.loads(e['after']['response_data']['S']) == json.loads(expected['response_data']['S']), e
            else:
                assert Decimal(e['after']['in_progress_expiration']['N']) == Decimal(expected['in_progress_expiration']['N']), e
    elif e['phase'] == 'delete':
        # Immediate recovery after a known handler error, not a future lease expiry.
        item = e['after']
        assert not item or (item.get('status', {}).get('S') != 'COMPLETED' and
                            float(item.get('in_progress_expiration', {}).get('N', '0')) <= time.time() * 1000), e
    else:
        assert e['after'].get('status', {}).get('S') == 'COMPLETED', e
        assert json.loads(e['after']['response_data']['S']) == {**e['request']['direct_result'], 'callback_number': 1}, e
    assert e['next'].get('status') == 'SUCCEEDED', e
    assert e['next_item'].get('status', {}).get('S') == 'COMPLETED', e
    assert json.loads(e['next_item']['response_data']['S']) == e['next']['result'], e


def test_callback_service_error_is_not_a_storage_retry():
    deployment = Deployment()
    event = {'order': uuid.uuid4().hex, 'warehouse': 'remote'}
    req = deployment.request(event, wait_s=0)
    req.update(audit_callback=True, callback_transport_error=True)
    output = deployment.invoke(req)
    observed = deployment.db.get_item(TableName=deployment.tables['EFFECTS_TABLE'],
        Key={'payload_key': {'S': digest(event)}, 'execution_id': {'S': req['execution_id']}},
        ConsistentRead=True).get('Item', {})
    assert output.get('error') == 'ClientError' and 'callback upstream unavailable' in output.get('detail', ''), output
    assert int(observed.get('callback_count', {}).get('N', '0')) == 1, (observed, output)
    recovery = deployment.request(event, wait_s=0) | {'direct_result': {'receipt': uuid.uuid4().hex}}
    assert deployment.invoke(recovery).get('status') == 'SUCCEEDED'
