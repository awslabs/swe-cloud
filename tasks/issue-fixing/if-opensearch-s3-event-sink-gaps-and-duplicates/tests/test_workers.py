"""End-to-end multiworker observations; no dependency on inbox representation."""
import collections
import concurrent.futures
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
from rig import RecoveryWire, Wire, http, provision, prepare_inbox

LOGS = Path('/logs/verifier')



@pytest.fixture(scope='module')
def workers():
    LOGS.mkdir(parents=True, exist_ok=True)
    try:
        endpoint, s3, bucket = provision()
    except Exception as exc:
        pytest.fail('environment: workers engine/S3 preflight failed: ' + repr(exc))
    token = uuid.uuid4().hex[:12]
    def keys(n):
        return [token + '-' + uuid.uuid4().hex[:12] for _ in range(n)]
    shared, interrupted, peer_fresh, final_fresh = keys(24), keys(20), keys(10), keys(10)
    poison = keys(1)[0]
    valid = shared + interrupted + peer_fresh + final_fresh
    class Gates(RecoveryWire):
        def __init__(self):
            super().__init__(endpoint, str(LOGS/'workers-boundaries.jsonl'), interrupted)
            self.context = threading.local()
            self.first_worker = self.retry_worker = None

        def log(self, **record):
            if record.get('kind') == 'boundary':
                record['worker'] = self.context.worker
                if record['stage'] == 1 and record['applied']:
                    self.first_worker = self.context.worker
                elif record['stage'] == 2:
                    self.retry_worker = self.context.worker
            super().log(**record)

    gates = Gates()
    gates.stage = 0

    class WorkerWire(Wire):
        def __init__(self, i):
            super().__init__(endpoint, {}, str(LOGS/f'workers-proxy-{i}.jsonl'))
            self.i = i

        def intercept_bulk(self, documents, body, content_type):
            if not any(d['key'] in gates.originals for d in documents):
                return None
            gates.context.worker = self.i
            return gates.intercept_bulk(documents, body, content_type)

    wire = [WorkerWire(i) for i in range(2)]
    # Outside the kernel ephemeral-port range used by the proxy listeners.
    ports = [18080, 18081]
    processes = [None, None]
    logs = [open(LOGS/f'workers-sink-{i}.log', 'w') for i in range(2)]
    env = {**os.environ, 'PYTHONPATH':'/app', 'PYTHONDONTWRITEBYTECODE':'1',
           'WRITE_ALIAS':'events', 'QUERY_TERM':'key', 'MAX_LATE_APPLY_S':'8',
           'DLQ_BUCKET':bucket, 'DLQ_PREFIX':'dlq/',
           'INBOX_BUCKET':bucket, 'INBOX_PREFIX':'accepted/' + token + '/'}
    prepare_inbox(s3, bucket, env['INBOX_PREFIX'])
    endpoints = [w.start(0) for w in wire]
    errors, ready_at, posted, first_seen = [], [], {}, {}
    stop = threading.Event()
    s3.put_object(Bucket=bucket, Key='unrelated/sentinel', Body=b'preserve')

    def start(i):
        workdir = tempfile.mkdtemp(prefix='worker-replacement-')
        processes[i] = subprocess.Popen(['python','-m','sink'], cwd=workdir,
            env={**env, 'OS_ENDPOINT':endpoints[i], 'WORKER_ID':f'slot-{i}',
                 'INTAKE_PORT':str(ports[i]), 'TMPDIR':workdir},
            stdout=logs[i], stderr=subprocess.STDOUT)
        until = time.monotonic() + 15
        while time.monotonic() < until:
            if processes[i].poll() is not None:
                raise RuntimeError(f'worker {i} exited during start')
            try:
                if http('GET', f'http://127.0.0.1:{ports[i]}/healthz').status_code == 200:
                    ready_at.append({'worker':i,'time':time.monotonic()})
                    return
            except Exception:
                pass
            time.sleep(.05)
        raise RuntimeError(f'worker {i} did not report healthy')

    def kill(i):
        if processes[i] is not None and processes[i].poll() is None:
            processes[i].kill()
            processes[i].wait(timeout=5)

    def post(i, batch, duplicate=False):
        docs = [{'key':key, 'ts':'not-a-date' if key == poison else '2026-10-23T12:00:00Z',
                 'payload':'event-' + key,
                 **({'potential_duplicate':True} if duplicate else {})} for key in batch]
        response = http('POST', f'http://127.0.0.1:{ports[i]}/events', json=docs)
        if response.status_code != 202:
            raise RuntimeError(f'worker {i} intake returned {response.status_code}')
        accepted = time.monotonic()
        for key in batch:
            posted.setdefault(key, accepted)

    def sample():
        while not stop.is_set():
            try:
                response = http('POST', endpoint+'/events/_search', json={
                    'size':0, 'aggs':{'keys':{'terms':{'field':'key','size':1000}}}})
                response.raise_for_status()
                for entry in response.json()['aggregations']['keys']['buckets']:
                    first_seen.setdefault(entry['key'], time.monotonic())
            except Exception as exc:
                errors.append('sampler: ' + repr(exc))
            stop.wait(.2)

    sampler = threading.Thread(target=sample, daemon=True)
    try:
        start(0)
        start(1)
        sampler.start()
        # Both snapshots are loaded before either receives these previously unseen
        # identities. No observed S3 schema or exact interleaving is required.
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(post, i, shared + [poison]) for i in range(2)]
            for future in futures:
                future.result()
        post(0, shared, True)
        post(1, shared, True)
        with gates.lock:
            gates.stage = 1
        post(1, interrupted)
        if not gates.first_applied.wait(5):
            raise RuntimeError('interrupted worker never reached applied boundary')
        failed = gates.first_worker
        peer = 1 - failed
        kill(failed)
        with gates.lock:
            gates.stage = 2
        gates.release_first.set()
        # A live peer sees redelivery of some work whose durable writer exited.
        # The remaining accepted originals are never resubmitted: recovery must
        # preserve their records while peers mutate sibling catalog entries.
        post(peer, interrupted[:8], True)
        post(peer, peer_fresh)
        start(failed)
        # Recovery and live peer redelivery use the same identity ownership.
        post(peer, interrupted[:8] + shared, True)
        if not gates.retry_waiting.wait(23):
            errors.append('no recovery retry reached the transport boundary')
        if gates.retry_worker is None:
            raise RuntimeError('missing recovery sender')
        failed = gates.retry_worker
        peer = 1 - failed
        kill(failed)
        with gates.lock:
            gates.stage = 3
        gates.release_retry.set()
        http('POST', endpoint+'/events/_rollover', json={}).raise_for_status()
        start(failed)
        final_ready = ready_at[-1]['time']
        post(peer, final_fresh)
        post(peer, interrupted[:8], True)
        post(failed, shared + peer_fresh + final_fresh, True)
        until = final_ready + 27
        while time.monotonic() < until:
            time.sleep(.1)
        for i in range(2):
            processes[i].send_signal(signal.SIGTERM)
        for i in range(2):
            try:
                processes[i].wait(timeout=5)
            except subprocess.TimeoutExpired:
                errors.append(f'worker {i} exceeded shutdown grace')
                kill(i)
    except Exception as exc:
        errors.append('worker execution: ' + repr(exc))
    finally:
        for i in range(2):
            kill(i)
        gates.release_first.set()
        gates.release_retry.set()
        stop.set()
        if sampler.is_alive():
            sampler.join(timeout=20)
        for w in wire:
            w.close()
        for stream in logs:
            stream.close()
    result = {'errors':errors, 'wire_errors':[e for w in wire + [gates] for e in w.errors],
              'valid':valid, 'shared':shared, 'interrupted':interrupted,
              'fresh':peer_fresh+final_fresh, 'poison':poison, 'posted':posted,
              'first_seen':first_seen, 'ready_at':ready_at,
              'records':sorted([r for w in wire + [gates] for r in w.records], key=lambda r:r['time']),
              'late_done':gates.late_done.is_set()}
    try:
        http('POST', endpoint+'/events/_refresh').raise_for_status()
        response = http('POST', endpoint+'/events/_search', json={'size':1000})
        response.raise_for_status()
        result['documents'] = response.json()['hits']['hits']
        objects = s3.list_objects_v2(Bucket=bucket, Prefix='dlq/').get('Contents', [])
        result['dlq'] = {item['Key']:json.loads(s3.get_object(Bucket=bucket, Key=item['Key'])['Body'].read())
                         for item in objects}
        result['sentinel'] = s3.get_object(Bucket=bucket, Key='unrelated/sentinel')['Body'].read().decode()
        result['settings'] = http('GET', endpoint+'/events-*/_settings').json()
        result['aliases'] = http('GET', endpoint+'/_alias/events').json()
    except Exception as exc:
        errors.append('worker final observations: ' + repr(exc))
    (LOGS/'worker-observations.json').write_text(json.dumps(result,indent=2))
    return result


def test_workers_execution(workers):
    assert not workers['errors'], workers['errors']
    assert not workers['wire_errors'], workers['wire_errors']
    assert len(workers['ready_at']) == 4 and workers['late_done']
    assert not [r for r in workers['records'] if r['kind'] == 'forbidden']


def test_shared_identity_once(workers):
    counts = collections.Counter(d['_source']['key'] for d in workers.get('documents',[]))
    assert counts == collections.Counter(workers['valid']), dict(counts)
    assert all(d['_source']['payload'] == 'event-' + d['_source']['key'] and
               d['_source']['ts'] == '2026-10-23T12:00:00Z' for d in workers['documents'])
    indexed = sum(s == 201 for r in workers['records'] if r['kind'] == 'applied' for s in r['statuses'])
    assert indexed == len(workers['valid']), indexed


def test_peer_delivery_and_recovery_slo(workers):
    assert len(workers['ready_at']) == 4
    first_bulk = {}
    for r in workers['records']:
        if r['kind'] == 'bulk':
            for key in r['keys']:
                first_bulk.setdefault(key,r['time'])
    fresh = workers['shared'] + workers['fresh'] + [workers['poison']]
    late_dispatch = {k:first_bulk.get(k,float('inf'))-workers['posted'][k] for k in fresh
                     if first_bulk.get(k,float('inf'))-workers['posted'][k] > 1.5}
    assert not late_dispatch, late_dispatch
    late = {k:workers['first_seen'].get(k,float('inf'))-workers['posted'][k]
            for k in workers['shared'] + workers['fresh']
            if workers['first_seen'].get(k,float('inf'))-workers['posted'][k] > 25}
    assert not late, late
    deadline = workers['ready_at'][-1]['time'] + 25
    late_recovery = {k:workers['first_seen'].get(k,float('inf'))-deadline
                     for k in workers['interrupted']
                     if workers['first_seen'].get(k,float('inf')) > deadline}
    assert not late_recovery, late_recovery


def test_workers_rejection_and_policy(workers):
    expected = 'dlq/' + workers['poison'] + '.json'
    assert set(workers.get('dlq',{})) == {expected}
    assert workers['dlq'][expected]['event'] == {
        'key':workers['poison'], 'ts':'not-a-date', 'payload':'event-'+workers['poison']}
    assert 'mapper_parsing_exception' in json.dumps(workers['dlq'][expected]['error'])
    assert workers.get('sentinel') == 'preserve'
    assert set(workers.get('aliases',{})) == {'events-000001','events-000002'}
    assert {s['settings']['index']['refresh_interval'] for s in workers.get('settings',{}).values()} == {'5s'}
