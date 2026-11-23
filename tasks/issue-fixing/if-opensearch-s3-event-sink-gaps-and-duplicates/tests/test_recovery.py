import collections
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

import pytest
from rig import RecoveryWire, http, provision, prepare_inbox

LOGS = Path('/logs/verifier')


@pytest.fixture(scope='module')
def recovery():
    LOGS.mkdir(parents=True, exist_ok=True)
    try:
        endpoint, s3, bucket = provision()
    except Exception as exc:
        pytest.fail('environment: recovery engine preflight failed: ' + repr(exc))
    token = uuid.uuid4().hex[:12]
    originals = [token + '-' + uuid.uuid4().hex[:10] for _ in range(32)]
    fresh2 = [token + '-' + uuid.uuid4().hex[:10] for _ in range(8)]
    fresh3 = [token + '-' + uuid.uuid4().hex[:10] for _ in range(8)]
    flagged = token + '-new'
    existing = token + '-existing'
    poison = token + '-rejected'
    valid = originals + fresh2 + fresh3 + [flagged, existing]
    inbox_prefix = 'accepted/' + token + '/'
    prepare_inbox(s3, bucket, inbox_prefix)
    sentinel_key, sentinel = 'platform/config.json', b'{"owner":"platform"}'
    s3.put_object(Bucket=bucket, Key=sentinel_key, Body=sentinel)
    # Stored before this inbox existed; its first receipt arrives flagged.
    http('POST', endpoint + '/events-000001/_doc', json={
        'key': existing, 'ts': '2026-10-23T12:00:00Z', 'payload': 'event-' + existing}).raise_for_status()
    http('POST', endpoint + '/events-000001/_refresh').raise_for_status()
    wire = RecoveryWire(endpoint, str(LOGS / 'recovery-proxy.jsonl'), originals)
    errors, first_seen, posted, ready_at = [], {}, {}, []
    stop = threading.Event()
    process = None
    output = open(LOGS / 'recovery-sink.log', 'w')
    env = {**os.environ, 'PYTHONPATH': '/app', 'PYTHONDONTWRITEBYTECODE': '1',
           'WRITE_ALIAS': 'events', 'QUERY_TERM': 'key', 'MAX_LATE_APPLY_S': '8',
           'DLQ_BUCKET': bucket, 'DLQ_PREFIX': 'dlq/',
           'INBOX_BUCKET': bucket, 'INBOX_PREFIX': inbox_prefix}

    def start():
        nonlocal process
        # No cwd is reused. Durable recovery is independently supplied by AWS.
        workdir = tempfile.mkdtemp(prefix='replacement-')
        process = subprocess.Popen(['python', '-m', 'sink'], cwd=workdir,
                                   env={**env, 'TMPDIR': workdir},
                                   stdout=output, stderr=subprocess.STDOUT)
        for _ in range(150):
            if process.poll() is not None:
                raise RuntimeError('replacement exited at startup')
            try:
                if http('GET', 'http://127.0.0.1:8080/healthz').status_code == 200:
                    ready_at.append(time.monotonic())
                    return
            except Exception:
                pass
            time.sleep(.1)
        raise RuntimeError('replacement never healthy')

    def kill():
        process.kill()
        process.wait(timeout=5)

    def post(keys, duplicate=False):
        documents = [{'key': key, 'ts': 'not-a-date' if key == poison else '2026-10-23T12:00:00Z',
                      'payload': 'event-' + key, **({'potential_duplicate': True} if duplicate else {})}
                     for key in keys]
        response = http('POST', 'http://127.0.0.1:8080/events', json=documents)
        if response.status_code != 202:
            raise RuntimeError('intake failed: ' + str(response.status_code))
        for key in keys:
            posted.setdefault(key, time.monotonic())

    def sample():
        while not stop.is_set():
            try:
                response = http('POST', endpoint + '/events/_search', json={
                    'size': 0, 'aggs': {'keys': {'terms': {'field': 'key', 'size': 1000}}}})
                response.raise_for_status()
                for entry in response.json()['aggregations']['keys']['buckets']:
                    first_seen.setdefault(entry['key'], time.monotonic())
            except Exception as exc:
                errors.append('sampler: ' + repr(exc))
            stop.wait(.3)

    sampler = threading.Thread(target=sample, daemon=True)
    try:
        env['OS_ENDPOINT'] = wire.start()
        start()
        sampler.start()
        post(originals)
        if not wire.first_applied.wait(5):
            raise RuntimeError('no initial applied request at the protocol boundary')
        # All originals have a 202. One bulk has reached the engine without any
        # response; others may still be queued or held without being applied.
        kill()
        with wire.lock:
            wire.stage = 2
        wire.release_first.set()
        start()
        post(fresh2)
        fresh2_accepted = time.monotonic()
        post(originals[:4], duplicate=True)
        if not wire.retry_waiting.wait(23):
            errors.append('recovery never dispatched an absent original key')
        remaining = fresh2_accepted + 1.5 - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        kill()
        with wire.lock:
            wire.stage = 3
        wire.release_retry.set()
        http('POST', endpoint + '/events/_rollover', json={}).raise_for_status()
        start()
        post(fresh3 + [poison])
        post([flagged, existing], duplicate=True)
        post(originals + fresh2, duplicate=True)
        until = ready_at[-1] + 27
        while time.monotonic() < until:
            time.sleep(.1)
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            errors.append('replacement did not exit within shutdown grace')
            kill()
    except Exception as exc:
        errors.append('recovery execution: ' + repr(exc))
    finally:
        if process is not None and process.poll() is None:
            kill()
        wire.release_first.set()
        wire.release_retry.set()
        stop.set()
        if sampler.is_alive():
            sampler.join(timeout=20)
        output.close()
        wire.close()
    result = {'errors': errors, 'wire_errors': wire.errors, 'valid': valid,
              'originals': originals, 'fresh2': fresh2, 'fresh3': fresh3, 'flagged': flagged,
              'poison': poison, 'posted': posted, 'first_seen': first_seen,
              'ready_at': ready_at, 'records': wire.records,
              'applied_keys': sorted(wire.applied_keys or []),
              'retry_keys': sorted(wire.retry_keys or []), 'late_done': wire.late_done.is_set()}
    try:
        http('POST', endpoint + '/events/_refresh').raise_for_status()
        response = http('POST', endpoint + '/events/_search', json={'size': 1000})
        response.raise_for_status()
        result['documents'] = response.json()['hits']['hits']
        objects = s3.list_objects_v2(Bucket=bucket, Prefix='dlq/').get('Contents', [])
        result['dlq'] = {item['Key']: json.loads(s3.get_object(Bucket=bucket, Key=item['Key'])['Body'].read())
                         for item in objects}
        result['sentinel'] = s3.get_object(Bucket=bucket, Key=sentinel_key)['Body'].read().decode()
        result['settings'] = http('GET', endpoint + '/events-*/_settings').json()
        result['aliases'] = http('GET', endpoint + '/_alias/events').json()
    except Exception as exc:
        errors.append('recovery final observations: ' + repr(exc))
    (LOGS / 'recovery-observations.json').write_text(json.dumps(result, indent=2))
    return result


def test_replacement_execution(recovery):
    assert not recovery['errors'], recovery['errors']
    assert not recovery['wire_errors'], recovery['wire_errors']
    assert len(recovery['ready_at']) == 3
    assert recovery['applied_keys'] and recovery['retry_keys'] and recovery['late_done']
    assert not [r for r in recovery['records'] if r['kind'] == 'forbidden']


def test_recovered_deliveries_once(recovery):
    counts = collections.Counter(doc['_source']['key'] for doc in recovery.get('documents', []))
    assert counts == collections.Counter(recovery['valid']), dict(counts)
    indexed = sum(status == 201 for record in recovery['records'] if record['kind'] == 'applied'
                  for status in record['statuses'])
    # The pre-existing key was stored directly, not through the sink.
    assert indexed == len(recovery['valid']) - 1, indexed
    assert all(doc['_source']['payload'] == 'event-' + doc['_source']['key'] and
               doc['_source']['ts'] == '2026-10-23T12:00:00Z'
               for doc in recovery.get('documents', []))


def test_recovery_visibility_and_fresh_intake(recovery):
    assert len(recovery['ready_at']) == 3
    deadline = recovery['ready_at'][-1] + 25
    late = {key: recovery['first_seen'].get(key, float('inf')) - deadline
            for key in recovery['originals'] + recovery['fresh2']
            if recovery['first_seen'].get(key, float('inf')) > deadline}
    assert not late, late
    first_bulk = {}
    for record in recovery['records']:
        if record['kind'] == 'bulk':
            for key in record['keys']:
                first_bulk.setdefault(key, record['time'])
    delayed = {key: first_bulk.get(key, float('inf')) - recovery['posted'][key]
               for key in recovery['fresh2'] + recovery['fresh3'] + [recovery['poison']]
               if first_bulk.get(key, float('inf')) - recovery['posted'][key] > 1.5}
    assert not delayed, delayed
    # Fresh work in the final uninterrupted process keeps the acceptance SLO.
    late_fresh = {key: recovery['first_seen'].get(key, float('inf')) - recovery['posted'][key]
                  for key in recovery['fresh3'] + [recovery['flagged']]
                  if recovery['first_seen'].get(key, float('inf')) - recovery['posted'][key] > 25}
    assert not late_fresh, late_fresh


def test_recovery_dlq_and_store_policy(recovery):
    expected = 'dlq/' + recovery['poison'] + '.json'
    assert set(recovery.get('dlq', {})) == {expected}, recovery.get('dlq')
    record = recovery['dlq'][expected]
    assert record['event'] == {'key': recovery['poison'], 'ts': 'not-a-date',
                               'payload': 'event-' + recovery['poison']}
    assert 'mapper_parsing_exception' in json.dumps(record['error'])
    assert recovery.get('sentinel') == '{"owner":"platform"}'
    assert set(recovery.get('aliases', {})) == {'events-000001', 'events-000002'}
    assert {s['settings']['index']['refresh_interval']
            for s in recovery.get('settings', {}).values()} == {'5s'}
