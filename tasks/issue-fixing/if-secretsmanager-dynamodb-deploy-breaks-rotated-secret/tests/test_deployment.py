"""Black-box service observations; no imports from the deployment tree."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import boto3
from botocore.config import Config
import pytest
from cdk_seed.cfn_deploy import deploy

ENDPOINT = os.environ.get('AWS_ENDPOINT_URL', 'http://localhost:4566')
APP = Path('/app')


def client(service):
    return boto3.client(service, endpoint_url=ENDPOINT, region_name='us-east-1',
        aws_access_key_id='test', aws_secret_access_key='test',
        config=Config(connect_timeout=3, read_timeout=5, retries={'max_attempts': 1}))


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Capture:
    """Forward real AWS requests and retain immutable operation observations."""
    def __init__(self, after=None):
        self.calls = []
        self.after = after
        owner = self
        upstream = urlparse(ENDPOINT)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                operation = self.headers.get('X-Amz-Target', '').split('.')[-1]
                headers = {k: v for k, v in self.headers.items()
                           if k.lower() not in ('host', 'connection', 'accept-encoding')}
                connection = http.client.HTTPConnection(upstream.hostname, upstream.port or 80,
                                                         timeout=15)
                try:
                    connection.request('POST', self.path, body=body, headers=headers)
                    response = connection.getresponse()
                    data = response.read()
                    request = json.loads(body)
                    owner.calls.append((operation, request, response.status))
                    if owner.after:
                        owner.after(operation, request, response.status)
                    self.send_response(response.status)
                    for key, value in response.getheaders():
                        if key.lower() not in ('content-length', 'transfer-encoding', 'connection'):
                            self.send_header(key, value)
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    try:
                        self.wfile.write(data)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                finally:
                    connection.close()

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    @property
    def endpoint(self):
        return 'http://127.0.0.1:' + str(self.server.server_port)


class Site:
    def __init__(self, tmp_path, bootstrap=True):
        self.path = tmp_path
        self.sm, self.ddb = client('secretsmanager'), client('dynamodb')
        suffix = uuid.uuid4().hex
        self.c = dict(resource_id='credential_' + suffix, secret_id='service/db/' + suffix,
            secret_string='boot-' + uuid.uuid4().hex, version_stages=['AWSCURRENT'],
            state_table='state_' + suffix, ledger_table='journal_' + suffix,
            db_table='db_' + suffix)
        deploy(client('cloudformation'), 'credential-test-seed-' + suffix,
               Path(__file__).parent / 'cdk_seed/seed.template.json',
               StateTable=self.c['state_table'], LedgerTable=self.c['ledger_table'],
               DbTable=self.c['db_table'])
        for table in ('state_table', 'ledger_table', 'db_table'):
            self.ddb.get_waiter('table_exists').wait(TableName=self.c[table])
        self.sm.create_secret(Name=self.c['secret_id'])
        self.config = tmp_path / 'site.json'
        self.config.write_text(json.dumps(self.c))
        self.known = self.versions()  # Preserve any service-created empty-secret history.
        if bootstrap:
            self.initial = self.put(self.c['secret_string'], ['AWSCURRENT'])
            self.set_state(self.initial, 1)
            self.finish_row(self.initial, 0)
            self.set_db(self.initial)

    def put(self, value=None, stages=None):
        token = str(uuid.uuid4())
        args = dict(SecretId=self.c['secret_id'], ClientRequestToken=token,
                    SecretString=value or ('password-' + uuid.uuid4().hex))
        if stages is not None:
            args['VersionStages'] = stages
        result = self.sm.put_secret_value(**args)
        self.known.add(result['VersionId'])
        return result['VersionId']

    def row(self, table, key, value):
        return self.ddb.get_item(TableName=self.c[table], Key={key: {'S': value}},
                                 ConsistentRead=True).get('Item')

    def state(self):
        return self.row('state_table', 'resource_id', self.c['resource_id'])

    def ledger(self):
        return self.row('ledger_table', 'secret_id', self.c['secret_id'])

    def db(self):
        return self.row('db_table', 'user', 'app')

    def set_state(self, token, serial):
        self.ddb.put_item(TableName=self.c['state_table'], Item={
            'resource_id': {'S': self.c['resource_id']},
            'secret_id': {'S': self.c['secret_id']}, 'version_id': {'S': token},
            'serial': {'N': str(serial)}})

    def finish_row(self, token, seq):
        self.ddb.put_item(TableName=self.c['ledger_table'], Item={
            'secret_id': {'S': self.c['secret_id']}, 'state': {'S': 'finished'},
            'finished_version': {'S': token}, 'pending_token': {'S': token},
            'seq': {'N': str(seq)}})

    def set_db(self, token):
        value = self.sm.get_secret_value(SecretId=self.c['secret_id'], VersionId=token)['SecretString']
        self.ddb.put_item(TableName=self.c['db_table'], Item={
            'user': {'S': 'app'}, 'password_sha256': {'S': digest(value)},
            'set_by_token': {'S': token}})

    def labels(self):
        return {k: sorted(v) for k, v in self.sm.describe_secret(
            SecretId=self.c['secret_id'])['VersionIdsToStages'].items()}

    def versions(self):
        args = dict(SecretId=self.c['secret_id'], IncludeDeprecated=True)
        versions = set()
        while True:
            page = self.sm.list_secret_version_ids(**args)
            versions.update(v['VersionId'] for v in page['Versions'])
            if not page.get('NextToken'):
                return versions
            args['NextToken'] = page['NextToken']

    def holder(self, label='AWSCURRENT'):
        found = [v for v, labels in self.labels().items() if label in labels]
        assert len(found) <= 1, (label, found)
        return found[0] if found else None

    def start_rotation(self):
        row = self.ledger()
        assert row['state']['S'] == 'finished'
        token = self.put(stages=['AWSPENDING'])
        self.ddb.update_item(TableName=self.c['ledger_table'],
            Key={'secret_id': {'S': self.c['secret_id']}},
            UpdateExpression='SET #s = :p, pending_token = :t, seq = seq + :n',
            ConditionExpression='#s = :f', ExpressionAttributeNames={'#s': 'state'},
            ExpressionAttributeValues={':p': {'S': 'pending'}, ':f': {'S': 'finished'},
                                       ':t': {'S': token}, ':n': {'N': '1'}})
        self.set_db(token)
        return token

    def publish(self, token):
        self.sm.update_secret_version_stage(SecretId=self.c['secret_id'],
            VersionStage='AWSCURRENT', MoveToVersionId=token, RemoveFromVersionId=self.holder())

    def acknowledge(self, token):
        self.ddb.update_item(TableName=self.c['ledger_table'],
            Key={'secret_id': {'S': self.c['secret_id']}},
            UpdateExpression='SET #s = :f, finished_version = :t',
            ConditionExpression='pending_token = :t', ExpressionAttributeNames={'#s': 'state'},
            ExpressionAttributeValues={':f': {'S': 'finished'}, ':t': {'S': token}})

    def rotate(self):
        token = self.start_rotation()
        self.publish(token)
        self.acknowledge(token)
        return token

    def snapshot(self):
        return self.labels(), self.versions(), self.state(), self.ledger(), self.db()

    def invoke(self, *args, after=None):
        with Capture(after=after) as capture:
            env = {**os.environ, 'AWS_ENDPOINT_URL': capture.endpoint,
                   'AWS_REGION': 'us-east-1', 'AWS_DEFAULT_REGION': 'us-east-1',
                   'AWS_ACCESS_KEY_ID': 'test', 'AWS_SECRET_ACCESS_KEY': 'test',
                   'PYTHONPATH': str(APP), 'PYTHONDONTWRITEBYTECODE': '1'}
            result = subprocess.run([str(APP / 'recon'), *map(str, args)],
                cwd=tempfile.mkdtemp(), env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, (result.stdout, result.stderr)
        return capture.calls

    def plan(self, target):
        before = self.snapshot()
        output = self.path / ('plan-' + uuid.uuid4().hex + '.json')
        calls = self.invoke('plan', '--config', self.config, '--out', output)
        assert self.snapshot() == before, 'planning changed service state'
        assert all(op.startswith(('Get', 'Describe', 'List', 'Query', 'Scan'))
                   for op, body, status in calls), calls
        change = json.loads(output.read_text())
        assert change['anchor_before'] == self.state()['version_id']['S']
        assert change['observed_holder'] == self.holder()
        assert change['target_version'] == target
        assert isinstance(change['actions'], list)
        return output

    def reconcile(self, target, previous, pending, match_db=True, saved=None):
        foreign = self.ledger(), self.db()
        versions = self.versions()
        old_serial = int(self.state()['serial']['N'])
        calls = self.invoke('apply', '--plan', saved) if saved else self.invoke(
            'apply', '--config', self.config)
        assert (self.ledger(), self.db()) == foreign, 'deployment changed rotation-owned records'
        assert self.versions() == versions == self.known, 'deployment created or removed a version'
        assert self.holder() == target
        assert self.holder('AWSPREVIOUS') == previous
        assert self.holder('AWSPENDING') == pending
        state = self.state()
        assert state['version_id']['S'] == target
        assert int(state['serial']['N']) >= old_serial
        current = self.sm.get_secret_value(SecretId=self.c['secret_id'], VersionStage='AWSCURRENT')
        assert current['VersionId'] == target
        if match_db:
            assert digest(current['SecretString']) == self.db()['password_sha256']['S']
        for op, body, status in calls:
            if status >= 300:
                continue  # A rejected optimistic move is safe; refreshed convergence is required.
            if op == 'UpdateSecretVersionStage':
                assert body['VersionStage'] == 'AWSCURRENT', body
                assert body.get('MoveToVersionId') == target, body
            elif op.startswith(('Put', 'Update', 'Delete', 'Create', 'BatchWrite', 'TransactWrite')):
                assert op in ('PutItem', 'UpdateItem', 'TransactWriteItems'), (op, body)
                # Outcome checks above cover arbitrary valid state write protocols.
                if op in ('PutItem', 'UpdateItem'):
                    assert body['TableName'] == self.c['state_table'], body
                else:
                    for operation in body['TransactItems']:
                        for kind, request in operation.items():
                            if kind != 'ConditionCheck':
                                assert request['TableName'] == self.c['state_table'], request


def test_initial_installation(tmp_path):
    site = Site(tmp_path, bootstrap=False)
    calls = site.invoke('apply', '--config', site.config)
    version = site.holder()
    assert version and site.versions() == site.known | {version}
    assert site.state()['version_id']['S'] == version
    assert site.sm.get_secret_value(SecretId=site.c['secret_id'],
        VersionStage='AWSCURRENT')['SecretString'] == site.c['secret_string']
    assert site.ledger() is None and site.db() is None
    site.known.add(version)
    site.reconcile(version, site.holder('AWSPREVIOUS'), None, match_db=False)


@pytest.mark.parametrize('iteration', range(2))
def test_deployment_cycle(tmp_path, iteration):
    site = Site(tmp_path)
    # Correct accidental changes even before the first rotation.
    drift = site.put()
    site.reconcile(site.initial, drift, None)


@pytest.mark.parametrize('iteration', range(2))
def test_service_update(tmp_path, iteration):
    site = Site(tmp_path)
    owner = site.rotate()
    drift = site.put()
    site.reconcile(owner, drift, owner)
    site.reconcile(owner, drift, owner)


@pytest.mark.parametrize('iteration', range(2))
def test_reviewed_change(tmp_path, iteration):
    site = Site(tmp_path)
    owner = site.rotate()
    site.set_state(owner, 2)
    drift = site.put()
    saved = site.plan(owner)
    new_owner = site.rotate()
    site.reconcile(new_owner, drift, new_owner, saved=saved)
    # Replaying the reviewed plan must not undo the already adopted version.
    site.reconcile(new_owner, drift, new_owner, saved=saved)


@pytest.mark.parametrize('iteration', range(2))
def test_worker_restart(tmp_path, iteration):
    site = Site(tmp_path)
    old_owner = site.rotate()
    site.set_state(old_owner, 2)
    token = site.start_rotation()
    site.publish(token)
    # Worker stopped after publication; completion journal is not acknowledged yet.
    if iteration == 0:
        # A console update displaces the published token before any deployment ran.
        previous = site.put()
    else:
        site.reconcile(token, old_owner, token)
        # Two console updates push the adopted token off AWSCURRENT and AWSPREVIOUS.
        site.put()
        previous = site.put()
    site.reconcile(token, previous, token)
    site.acknowledge(token)
    site.reconcile(token, previous, token)


@pytest.mark.parametrize('iteration', range(1))
def test_worker_resume(tmp_path, iteration):
    site = Site(tmp_path)
    owner = site.rotate()
    site.set_state(owner, 2)
    token = site.start_rotation()
    for _ in range(1 + iteration):
        drift = site.put()
    site.reconcile(owner, drift, token, match_db=False)
    site.publish(token)
    site.acknowledge(token)
    site.reconcile(token, owner, token)


@pytest.mark.parametrize('iteration', range(2))
def test_resource_refresh(tmp_path, iteration):
    site = Site(tmp_path)
    owner = site.rotate()
    site.rotate()
    current = site.holder()
    site.set_state(str(uuid.uuid4()), 9)
    site.reconcile(current, owner, current)


def destination(source, path, managed):
    """Another existing secret assigned to the same deployment address."""
    path.mkdir()
    other = Site(path, bootstrap=managed)
    other.c['state_table'] = source.c['state_table']
    other.c['resource_id'] = source.c['resource_id']
    other.config.write_text(json.dumps(other.c))
    return other


def credential_snapshot(site):
    # The address's deployment row is shared; these belong to the secret itself.
    return site.labels(), site.versions(), site.ledger(), site.db()


def assert_retired(site, saved, peers):
    before = [peer.snapshot() for peer in peers]
    changed = []

    def observe(op, body, status):
        if status < 300 and not op.startswith(('Get', 'Describe', 'List', 'Query', 'Scan')):
            if [peer.snapshot() for peer in peers] != before:
                changed.append((op, body))

    site.invoke('apply', '--plan', saved, after=observe)
    assert [peer.snapshot() for peer in peers] == before, 'retired plan changed AWS state'
    assert not changed, ('retired plan temporarily changed AWS state', changed)


def test_replacement_of_backing_secret(tmp_path):
    old = Site(tmp_path)
    old.rotate()
    old_snapshot = credential_snapshot(old)
    new = destination(old, tmp_path / 'destination', managed=True)
    owner = new.rotate()
    drift = new.put()
    reviewed = new.plan(owner)
    new.reconcile(owner, drift, owner, saved=reviewed)
    assert new.state()['secret_id']['S'] == new.c['secret_id']
    assert credential_snapshot(old) == old_snapshot
    # The plan that performed the replacement is retired too. Its duplicate
    # delivery must not become a fresh authorization to repair later drift.
    new.put()
    assert_retired(new, reviewed, [old, new])


def test_replacement_bootstraps_only_destination(tmp_path):
    old = Site(tmp_path)
    stale = old.plan(old.initial)
    old_snapshot = credential_snapshot(old)
    new = destination(old, tmp_path / 'destination', managed=False)
    before = new.versions()
    new.invoke('apply', '--config', new.config)
    initial = new.holder()
    assert initial and new.versions() == before | {initial}
    assert new.sm.get_secret_value(SecretId=new.c['secret_id'], VersionId=initial)[
        'SecretString'] == new.c['secret_string']
    assert new.state()['version_id']['S'] == initial
    assert new.state()['secret_id']['S'] == new.c['secret_id']
    assert new.ledger() is None and new.db() is None
    assert credential_snapshot(old) == old_snapshot
    new.known.add(initial)
    new.initial = initial
    new.finish_row(initial, 0)
    new.set_db(initial)
    assert_retired(old, stale, [old, new])
    # Rotations and serial changes within the new binding do not retire plans.
    saved = new.plan(initial)
    owner = new.rotate()
    drift = new.put()
    new.reconcile(owner, drift, owner)
    later_drift = new.put()
    new.reconcile(owner, later_drift, owner, saved=saved)
    assert credential_snapshot(old) == old_snapshot


def test_return_to_prior_secret_does_not_revive_queue(tmp_path):
    old = Site(tmp_path)
    owner = old.rotate()
    old.set_state(owner, 2)
    stale_old = old.plan(owner)
    new = destination(old, tmp_path / 'destination', managed=True)
    new_owner = new.rotate()
    replacement = new.plan(new_owner)
    new.reconcile(new_owner, new.holder('AWSPREVIOUS'), new_owner, saved=replacement)
    # Independent rotation on a departed secret still owns that secret.
    owner = old.rotate()
    before_new = credential_snapshot(new)
    old.reconcile(owner, old.holder('AWSPREVIOUS'), owner)
    assert old.state()['secret_id']['S'] == old.c['secret_id']
    assert credential_snapshot(new) == before_new
    old.put()
    # Same name as the queued plan; different incarnation of the address.
    assert_retired(old, stale_old, [old, new])
    # Same source/destination names as the earlier approved replacement, but
    # that approval was consumed before the return to the old secret.
    assert_retired(new, replacement, [old, new])
    old.reconcile(owner, old.holder(), owner)


def test_superseded_replacement_is_not_a_new_request(tmp_path):
    old = Site(tmp_path)
    first = destination(old, tmp_path / 'first', managed=True)
    second = destination(old, tmp_path / 'second', managed=True)
    queued = second.plan(second.initial)
    first.reconcile(first.initial, first.holder('AWSPREVIOUS'), None)
    assert_retired(second, queued, [old, first, second])



def interrupt_after_write(site, saved, operation):
    fired = threading.Event()
    process = None

    def after(op, body, status):
        matches = op == operation
        if operation == 'state':
            matches = (op in ('PutItem', 'UpdateItem', 'DeleteItem') and
                       body.get('TableName') == site.c['state_table'])
            if op == 'TransactWriteItems':
                matches = any(kind in ('Put', 'Update', 'Delete') and
                    request.get('TableName') == site.c['state_table']
                    for item in body['TransactItems'] for kind, request in item.items())
        if operation == 'state' and op == 'BatchWriteItem':
            matches = site.c['state_table'] in body.get('RequestItems', {})
        if not fired.is_set() and status < 300 and matches:
            fired.set()
            process.kill()

    with Capture(after=after) as capture:
        env = {**os.environ, 'AWS_ENDPOINT_URL': capture.endpoint,
               'AWS_REGION': 'us-east-1', 'AWS_DEFAULT_REGION': 'us-east-1',
               'AWS_ACCESS_KEY_ID': 'test', 'AWS_SECRET_ACCESS_KEY': 'test',
               'PYTHONPATH': str(APP), 'PYTHONDONTWRITEBYTECODE': '1'}
        process = subprocess.Popen([str(APP / 'recon'), 'apply', '--plan', str(saved)],
            cwd=tempfile.mkdtemp(), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True)
        try:
            stdout, stderr = process.communicate(timeout=30)
        except BaseException:
            process.kill()
            process.communicate()
            raise
    assert fired.is_set(), ('replacement did not reach expected AWS operation', operation,
                            stdout, stderr, capture.calls)
    assert process.returncode != 0


def test_interrupted_binding_commit(tmp_path):
    old = Site(tmp_path)
    old_snapshot = credential_snapshot(old)
    new = destination(old, tmp_path / 'destination', managed=True)
    owner = new.rotate()
    drift = new.put()
    reviewed = new.plan(owner)
    # The kill is after the service accepted a state write, not a timeout race.
    # Implementations may write an intent first, or commit only after label work.
    interrupt_after_write(new, reviewed, 'state')
    new.reconcile(owner, drift, owner, saved=reviewed)
    assert new.state()['secret_id']['S'] == new.c['secret_id']
    assert credential_snapshot(old) == old_snapshot
    new.put()
    assert_retired(new, reviewed, [old, new])


def test_interrupted_destination_creation(tmp_path):
    old = Site(tmp_path)
    old_snapshot = credential_snapshot(old)
    new = destination(old, tmp_path / 'destination', managed=False)
    reviewed = new.plan(None)
    baseline = new.versions()
    interrupt_after_write(new, reviewed, 'PutSecretValue')
    created = new.versions() - baseline
    assert len(created) == 1
    new.invoke('apply', '--plan', reviewed)
    assert new.versions() - baseline == created, 'retry cloned the initial credential'
    assert new.holder() == next(iter(created))
    assert new.state()['secret_id']['S'] == new.c['secret_id']
    assert new.state()['version_id']['S'] == new.holder()
    assert new.sm.get_secret_value(SecretId=new.c['secret_id'],
        VersionStage='AWSCURRENT')['SecretString'] == new.c['secret_string']
    assert new.ledger() is None and new.db() is None
    assert credential_snapshot(old) == old_snapshot
    assert_retired(new, reviewed, [old, new])
