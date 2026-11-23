import concurrent.futures
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import random
import select
import socket
import subprocess
import sys
import tempfile
import time
import uuid
import boto3
from botocore.config import Config
import pytest
from cdk_seed.cfn_deploy import deploy

APP = '/app'
LOG_ROOT = Path('/logs/verifier')
BOOTSTRAP = str(Path(__file__).with_name('invocation.py'))


def client():
    return boto3.client('dynamodb', endpoint_url=os.environ.get('AWS_ENDPOINT_URL', 'http://localhost:4566'),
                        region_name='us-east-1', aws_access_key_id='test', aws_secret_access_key='test',
                        config=Config(connect_timeout=2, read_timeout=4, retries={'max_attempts': 1}))


def digest(event):
    return hashlib.sha256(json.dumps(event, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def until(at):
    while time.monotonic() < at:
        time.sleep(min(0.1, at - time.monotonic()))


class Deployment:
    def __init__(self):
        self.db = client()
        suffix = uuid.uuid4().hex[:14]
        self.tables = {'DISPATCH_TABLE': 'members_' + suffix,
                       'EXECUTION_TABLE': 'executions_' + suffix,
                       'IDEMPOTENCY_TABLE': 'orders_' + suffix,
                       'CHECKPOINT_TABLE': 'history_' + suffix, 'EFFECTS_TABLE': 'dispatch_' + suffix}
        try:
            self.db.list_tables()
            cfn = boto3.client('cloudformation', endpoint_url=self.db.meta.endpoint_url,
                               region_name=self.db.meta.region_name,
                               aws_access_key_id='test', aws_secret_access_key='test')
            deploy(cfn, 'dispatch-' + suffix, Path(__file__).parent / 'cdk_seed/seed.template.json',
                   DispatchTable=self.tables['DISPATCH_TABLE'],
                   ExecutionTable=self.tables['EXECUTION_TABLE'],
                   IdempotencyTable=self.tables['IDEMPOTENCY_TABLE'],
                   CheckpointTable=self.tables['CHECKPOINT_TABLE'], EffectsTable=self.tables['EFFECTS_TABLE'])
            for name in self.tables.values():
                self.db.get_waiter('table_exists').wait(TableName=name)
        except Exception as exc:
            pytest.fail('environment: DynamoDB preflight failed: ' + repr(exc))

    def request(self, event, start_ms=None, timeout_ms=45000, wait_s=15):
        return {'event': event, 'execution_id': str(uuid.uuid4()),
                'execution_start_ms': start_ms if start_ms is not None else int(time.time() * 1000),
                'execution_timeout_ms': timeout_ms, 'invocation_timeout_s': 4, 'wait_s': wait_s}

    def item(self, event):
        return self.db.get_item(TableName=self.tables['IDEMPOTENCY_TABLE'],
                                Key={'id': {'S': digest(event)}}, ConsistentRead=True).get('Item', {})

    def effects(self, event):
        rows = self.db.query(TableName=self.tables['EFFECTS_TABLE'],
                             KeyConditionExpression='payload_key = :p',
                             ExpressionAttributeValues={':p': {'S': digest(event)}},
                             ConsistentRead=True)['Items']
        return sorted(row['execution_id']['S'] for row in rows)

    def issue(self, request):
        request = {**request, 'invocation_id': str(uuid.uuid4())}
        self.db.put_item(TableName=self.tables['EXECUTION_TABLE'], Item={
            'execution_id': {'S': request['execution_id']},
            'active_invocation_id': {'S': request['invocation_id']}})
        return request

    def invoke(self, request, kill=False):
        request = self.issue(request)
        # Each role gets its own working directory, interpreter, client and environment.
        with tempfile.TemporaryDirectory(prefix='order-inv-') as cwd:
            env = {**os.environ, **self.tables, 'PYTHONPATH': APP, 'PYTHONDONTWRITEBYTECODE': '1',
                   'AWS_ENDPOINT_URL': os.environ.get('AWS_ENDPOINT_URL', 'http://localhost:4566'),
                   'AWS_ACCESS_KEY_ID': 'test', 'AWS_SECRET_ACCESS_KEY': 'test',
                   'AWS_DEFAULT_REGION': 'us-east-1'}
            env.pop('SEAM_SOCKET', None)
            listener = None
            if kill:
                listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                listener.bind(cwd + '/gate')
                listener.listen(1)
                env['SEAM_SOCKET'] = cwd + '/gate'
            started = time.monotonic()
            process = subprocess.Popen([sys.executable, BOOTSTRAP, json.dumps(request)],
                                       cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                if listener:
                    end = time.monotonic() + request.get('invocation_timeout_s', 4) - 0.2
                    while time.monotonic() < end and process.poll() is None:
                        ready, _, _ = select.select([listener], [], [], 0.05)
                        if ready:
                            conn, _ = listener.accept()
                            with conn:
                                conn.settimeout(1)
                                data = b''
                                while b'\n' not in data:
                                    part = conn.recv(4096)
                                    if not part:
                                        break
                                    data += part
                                announcement = json.loads(data.splitlines()[0])
                                time.sleep(0.15)
                                process.kill()
                                process.communicate(timeout=2)
                                return {'status': 'KILLED', 'announcement': announcement}
                try:
                    stdout, stderr = process.communicate(timeout=max(0.1, request.get('invocation_timeout_s', 4) - (time.monotonic() - started)))
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, stderr = process.communicate()
                    return {'status': 'FAILED', 'error': 'InvocationTimeout', 'stderr': stderr}
                if process.returncode:
                    return {'status': 'FAILED', 'error': 'InvocationProcess', 'stdout': stdout, 'stderr': stderr}
                try:
                    return json.loads(stdout.strip().splitlines()[-1])
                except (IndexError, ValueError):
                    return {'status': 'FAILED', 'error': 'InvocationProtocol', 'stdout': stdout, 'stderr': stderr}
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
                if listener:
                    listener.close()

    def finish(self, request):
        deadline = request['execution_start_ms'] + request['execution_timeout_ms']
        for _ in range(8):
            if time.time() * 1000 >= deadline:
                return {'status': 'TIMED_OUT'}
            output = self.invoke(request)
            if output.get('status') != 'SUSPENDED':
                return output
            resume = min(output['resume_at_ms'], deadline)
            time.sleep(max(0, (resume - time.time() * 1000) / 1000))
        return {'status': 'FAILED', 'error': 'TooManySuspensions'}


@pytest.fixture(scope='module', params=[0, 1])
def run(request):
    deployment = Deployment()
    rng = random.Random(uuid.uuid4().int)
    events = {name: {'order': uuid.uuid4().hex, 'warehouse': rng.choice(['north', 'south']), 'batch': request.param}
              for name in 'abc'}
    base = time.monotonic() + 0.25
    start_ms = int(time.time() * 1000 + 250)
    requests = {name: deployment.request(events[name], start_ms=start_ms) for name in 'abc'}
    jitter = {name: rng.uniform(0, 0.35) for name in 'abcde'}
    observations = {'requests': requests, 'events': events, 'results': {}, 'crashes': {}}

    def a():
        until(base + jitter['a'])
        observations['crashes']['a'] = deployment.invoke(requests['a'], kill=True)
        time.sleep(1)
        return deployment.finish(requests['a'])

    def b():
        until(base + jitter['b'])
        return deployment.finish(requests['b'])

    def c():
        until(base + 8 + jitter['c'])
        observations['crashes']['c1'] = deployment.invoke(requests['c'], kill=True)
        time.sleep(12)
        observations['crashes']['c2'] = deployment.invoke(requests['c'], kill=True)
        until(base + 45)
        return {'status': 'TIMED_OUT'}

    def rival(name, event_name, offset):
        until(base + offset + jitter.get(name, 0))
        req = deployment.request(events[event_name])
        requests[name] = req
        return deployment.finish(req)

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futures = {'a': pool.submit(a), 'b': pool.submit(b), 'c': pool.submit(c),
                   'd': pool.submit(rival, 'd', 'b', 8),
                   'e': pool.submit(rival, 'e', 'c', 30),
                   'f': pool.submit(rival, 'f', 'c', 48)}
        until(base + 10)
        observations['snapshot_b'] = deployment.item(events['b'])
        until(base + 31)
        observations['snapshot_c'] = deployment.item(events['c'])
        for name, future in futures.items():
            try:
                observations['results'][name] = future.result(timeout=70)
            except Exception as exc:
                observations['results'][name] = {'status': 'DRIVER_ERROR', 'detail': repr(exc)}
    observations['effects'] = {name: deployment.effects(event) for name, event in events.items()}
    observations['final'] = {name: deployment.item(event) for name, event in events.items()}
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    (LOG_ROOT / ('schedule-' + str(request.param) + '.json')).write_text(json.dumps(observations, indent=2))
    return deployment, observations


def test_continuations(run):
    _, evidence = run
    for name in ['a', 'b', 'f']:
        assert evidence['results'][name]['status'] == 'SUCCEEDED', (name, evidence['results'])
    assert evidence['results']['c']['status'] == 'TIMED_OUT'


def test_pre_checkpoint_invocations(run):
    _, evidence = run
    for name in ['a', 'c1', 'c2']:
        assert evidence['crashes'][name]['status'] == 'KILLED', evidence['crashes']
        facts = evidence['crashes'][name]['announcement']
        assert facts['seam'] == 'handler_entry'
        assert facts['operations_count'] == 1 and facts['mode'] == 'EXECUTION', facts


def test_competing_submissions(run):
    _, evidence = run
    for name in ['d', 'e']:
        result = evidence['results'][name]
        assert result.get('status') == 'FAILED' and result.get('error') == 'AlreadyInProgress', (name, result)


def test_dispatch_ledger(run):
    _, evidence = run
    for event_name, owner_name in [('a', 'a'), ('b', 'b'), ('c', 'f')]:
        assert evidence['effects'][event_name] == [evidence['requests'][owner_name]['execution_id']], evidence


@pytest.mark.parametrize('name', ['b', 'c'])
def test_inflight_operational_record(run, name):
    _, evidence = run
    item = evidence['snapshot_' + name]
    owner = evidence['requests'][name]
    assert item.get('status', {}).get('S') == 'INPROGRESS', item
    assert item.get('owner_execution_id', {}).get('S') == owner['execution_id'], item
    deadline = owner['execution_start_ms'] + owner['execution_timeout_ms']
    assert abs(Decimal(item.get('in_progress_expiration', {}).get('N', '0')) - deadline) <= 1500, item


def test_terminal_records(run):
    _, evidence = run
    for event_name, owner_name in [('a', 'a'), ('b', 'b'), ('c', 'f')]:
        item = evidence['final'][event_name]
        assert item.get('status', {}).get('S') == 'COMPLETED', (event_name, item)
        assert item.get('owner_execution_id', {}).get('S') == evidence['requests'][owner_name]['execution_id'], item
        assert json.loads(item['response_data']['S']) == evidence['results'][owner_name].get('result'), item


def test_completed_response_cache(run):
    deployment, evidence = run
    for name, owner in [('a', 'a'), ('b', 'b'), ('c', 'f')]:
        before = deployment.item(evidence['events'][name])
        output = deployment.invoke(deployment.request(evidence['events'][name], wait_s=0))
        assert output.get('status') == 'SUCCEEDED', output
        assert output.get('result') == evidence['results'][owner].get('result'), output
        after = deployment.item(evidence['events'][name])
        for field in ['status', 'owner_execution_id']:
            assert after.get(field) == before.get(field), (field, before, after)
        assert json.loads(after['response_data']['S']) == json.loads(before['response_data']['S'])
        assert deployment.effects(evidence['events'][name]) == evidence['effects'][name]


def test_handler_error_cleanup():
    deployment = Deployment()
    event = {'order': uuid.uuid4().hex, 'warehouse': 'west'}
    bad = deployment.request(event, wait_s=0)
    bad['handler_error'] = True
    output = deployment.invoke(bad)
    assert output.get('error') == 'ValueError', output
    assert not deployment.effects(event), deployment.effects(event)
    good = deployment.request(event, wait_s=0)
    output = deployment.finish(good)
    assert output.get('status') == 'SUCCEEDED', output
    assert deployment.effects(event) == [good['execution_id']]
