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
from rig import Wire, http, provision, prepare_inbox

LOGS = Path('/logs/verifier')


def search(endpoint):
    response = http('POST', endpoint + '/events/_search', json={
        'size': 0, 'aggs': {'keys': {'terms': {'field': 'key', 'size': 1000}}}})
    response.raise_for_status()
    return response.json()['aggregations']['keys']['buckets']


@pytest.fixture(scope='module')
def incident():
    LOGS.mkdir(parents=True, exist_ok=True)
    # A development instance left running by an engineer must not own the intake port.
    old_sinks = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            args = (entry / 'cmdline').read_bytes().split(b'\0')
            if len(args) >= 3 and Path(os.fsdecode(args[0])).name.startswith('python') and args[1:3] == [b'-m', b'sink']:
                pid = int(entry.name)
                os.kill(pid, signal.SIGTERM)
                old_sinks.append(pid)
        except (OSError, ValueError):
            pass
    until = time.monotonic() + 5
    while old_sinks and time.monotonic() < until:
        old_sinks = [pid for pid in old_sinks if Path('/proc', str(pid)).exists()]
        if old_sinks:
            time.sleep(.1)
    for pid in old_sinks:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        endpoint, s3, bucket = provision()
        assert http('GET', endpoint + '/events/_settings').ok
        probe = http('POST', endpoint + '/_msearch', data=
                     '{"index":"events"}\n{"size":0}\n',
                     headers={'Content-Type': 'application/x-ndjson'})
        probe.raise_for_status()
        assert 'error' not in probe.json()['responses'][0]
        s3.list_objects_v2(Bucket=bucket)
    except Exception as exc:
        pytest.fail('environment: engine/S3 preflight failed: ' + repr(exc))
    prepare_inbox(s3, bucket, 'inbox/')
    # Identities are independent of source fixtures and solver code.
    prefix = uuid.uuid4().hex[:8]
    keys = [prefix + '-' + uuid.uuid4().hex[:10] for _ in range(240)]
    poison = prefix + '-rejected'
    triggers = dict(zip(('one', 'two', 'three', 'four'), [keys[i] for i in (40, 130, 170, 200)]))
    wire = Wire(endpoint, triggers, str(LOGS / 'proxy.jsonl'))
    errors, first_seen, dlq_seen, posted = [], {}, {}, {}
    sampler_stop = threading.Event()
    process = None
    output = open(LOGS / 'sink.log', 'w')
    try:
        proxy = wire.start()
        env = {**os.environ, 'PYTHONPATH': '/app', 'PYTHONDONTWRITEBYTECODE': '1',
               'OS_ENDPOINT': proxy, 'WRITE_ALIAS': 'events', 'QUERY_TERM': 'key',
               'DLQ_BUCKET': bucket, 'DLQ_PREFIX': 'dlq/', 'MAX_LATE_APPLY_S': '8'}
        process = subprocess.Popen(['python', '-m', 'sink'], cwd=tempfile.mkdtemp(prefix='sink-run-'),
                                   env=env, stdout=output, stderr=subprocess.STDOUT)
        for _ in range(100):
            try:
                if http('GET', 'http://127.0.0.1:8080/healthz').status_code == 200:
                    break
            except Exception:
                pass
            if process.poll() is not None:
                raise RuntimeError('sink exited during startup')
            time.sleep(.1)
        else:
            raise RuntimeError('sink not ready')

        def sample():
            while not sampler_stop.is_set():
                try:
                    for item in search(endpoint):
                        first_seen.setdefault(item['key'], time.monotonic())
                    for item in s3.list_objects_v2(Bucket=bucket, Prefix='dlq/').get('Contents', []):
                        dlq_seen.setdefault(item['Key'], time.monotonic())
                except Exception as exc:
                    errors.append('sampler: ' + repr(exc))
                sampler_stop.wait(.5)

        sampler = threading.Thread(target=sample, daemon=True)
        sampler.start()
        timeline = [(i / 12, key, False) for i, key in enumerate(keys)]
        timeline += [(70 / 12 + .5, keys[70], True), (15.5, keys[10], True), (12, poison, False)]
        begin = time.monotonic()
        for offset, key, duplicate in sorted(timeline):
            remaining = begin + offset - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            event = {'key': key, 'ts': 'not-a-date' if key == poison else '2026-10-23T12:00:00Z',
                     'payload': 'event-' + key}
            if duplicate:
                event['potential_duplicate'] = True
            try:
                response = http('POST', 'http://127.0.0.1:8080/events', json=[event])
                if response.status_code != 202:
                    errors.append(f'intake rejected {key}: {response.status_code}')
                else:
                    posted.setdefault(key, time.monotonic())
            except Exception as exc:
                errors.append('intake: ' + repr(exc))
        remaining = begin + 45 - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        if process.poll() is not None:
            errors.append('sink exited before SIGTERM')
        else:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                errors.append('sink did not exit within shutdown grace')
                process.kill()
                process.wait(timeout=5)
        sampler_stop.set()
        sampler.join(timeout=20)
    except Exception as exc:
        errors.append('execution: ' + repr(exc))
    finally:
        sampler_stop.set()
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        output.close()
        wire.close()
    result = {'keys': keys, 'poison': poison, 'errors': errors, 'wire_errors': wire.errors,
              'first_seen': first_seen, 'posted': posted, 'dlq_seen': dlq_seen,
              'groups': wire.groups, 'records': wire.records}
    try:
        alias = http('GET', endpoint + '/_alias/events')
        alias.raise_for_status()
        result['alias'] = alias.json()
        settings = http('GET', endpoint + '/events-*/_settings')
        settings.raise_for_status()
        result['settings'] = settings.json()
        http('POST', endpoint + '/events/_refresh').raise_for_status()
        duplicate_response = http('POST', endpoint + '/events/_search', json={
            'size': 0, 'aggs': {'duplicates': {'terms': {
                'field': 'key', 'size': 1000, 'min_doc_count': 2}}}})
        duplicate_response.raise_for_status()
        result['duplicates'] = duplicate_response.json()['aggregations']['duplicates']['buckets']
        documents = http('POST', endpoint + '/events/_search', json={'size': 1000, 'query': {'match_all': {}}})
        documents.raise_for_status()
        result['documents'] = documents.json()['hits']['hits']
        count = http('GET', endpoint + '/events/_count')
        count.raise_for_status()
        result['count'] = count.json()['count']
        objects = s3.list_objects_v2(Bucket=bucket, Prefix='dlq/').get('Contents', [])
        result['dlq'] = {obj['Key']: json.loads(s3.get_object(Bucket=bucket, Key=obj['Key'])['Body'].read())
                         for obj in objects}
    except Exception as exc:
        errors.append('final observations: ' + repr(exc))
    (LOGS / 'observations.json').write_text(json.dumps(result, indent=2))
    return result


def test_execution_contract(incident):
    assert not incident['errors'], incident['errors']
    assert not incident['wire_errors'], incident['wire_errors']
    assert set(incident['groups']) == {'one', 'two', 'three', 'four'}, incident['groups']
    assert not [r for r in incident['records'] if r['kind'] == 'forbidden']
    first_bulk = {}
    for record in incident['records']:
        if record['kind'] == 'bulk':
            for key in record['keys']:
                first_bulk.setdefault(key, record['time'])
    # 0.5 seconds accommodates scheduling overhead, beyond the one-second contract.
    late = {k: first_bulk.get(k, float('inf')) - t for k, t in incident['posted'].items()
            if first_bulk.get(k, float('inf')) - t > 1.5}
    assert not late, 'first dispatch delayed: ' + repr(late)


def test_store_policy(incident):
    alias = incident.get('alias', {})
    assert set(alias) == {'events-000001', 'events-000002'}, alias
    assert alias['events-000002']['aliases']['events']['is_write_index'] is True
    assert alias['events-000001']['aliases']['events'].get('is_write_index') is False
    assert {s['settings']['index']['refresh_interval'] for s in incident.get('settings', {}).values()} == {'5s'}


def test_exactly_once_documents(incident):
    counts = collections.Counter(doc['_source']['key'] for doc in incident.get('documents', []))
    assert counts == collections.Counter(incident['keys']), dict(counts)
    assert incident.get('duplicates') == [], incident.get('duplicates')
    indexed = sum(status == 201 for record in incident['records'] if record['kind'] == 'applied'
                  for status in record['statuses'])
    assert incident.get('count') == 240 == indexed, (incident.get('count'), indexed)
    assert all(doc['_source']['payload'] == 'event-' + doc['_source']['key'] and
               doc['_source']['ts'] == '2026-10-23T12:00:00Z' for doc in incident.get('documents', []))


def test_historical_locations(incident):
    locations = collections.defaultdict(list)
    for doc in incident.get('documents', []):
        locations[doc['_source']['key']].append(doc['_index'])
    for rule in ('one', 'two'):
        assert rule in incident['groups'], incident['groups']
        wrong = {key: locations[key] for key in incident['groups'][rule]
                 if locations[key] != ['events-000001']}
        assert not wrong, wrong
    assert locations[incident['keys'][200]] == ['events-000002']


def test_dead_letter(incident):
    expected = 'dlq/' + incident['poison'] + '.json'
    assert set(incident.get('dlq', {})) == {expected}, incident.get('dlq')
    record = incident['dlq'][expected]
    assert record['event'] == {'key': incident['poison'], 'ts': 'not-a-date',
                               'payload': 'event-' + incident['poison']}
    assert record['error'] and 'mapper_parsing_exception' in json.dumps(record['error'])
    assert incident['dlq_seen'].get(expected, float('inf')) - incident['posted'][incident['poison']] <= 25


def test_delivery_deadline(incident):
    missing_or_late = {key: incident['first_seen'].get(key, float('inf')) - incident['posted'].get(key, 0)
                       for key in incident['keys']
                       if incident['first_seen'].get(key, float('inf')) - incident['posted'].get(key, 0) > 25}
    assert not missing_or_late, missing_or_late
