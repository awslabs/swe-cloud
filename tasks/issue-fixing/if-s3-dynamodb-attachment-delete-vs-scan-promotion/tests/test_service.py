import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
import pytest
from relay import Relay, Rule
from cdk_seed.cfn_deploy import deploy

DIRECT = 'http://localhost:4566'
CONFIG = Config(region_name='us-east-1', retries={'max_attempts': 0},
                connect_timeout=3, read_timeout=10, s3={'addressing_style': 'path'})


def client(service):
    return boto3.client(service, endpoint_url=DIRECT, aws_access_key_id='test',
                        aws_secret_access_key='test', config=CONFIG)


def eventually(check, seconds=20, message='condition did not converge'):
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        try:
            value = check()
            if value:
                return value
        except (ClientError, AssertionError) as error:
            last = error
        time.sleep(.15)
    raise AssertionError(f'{message}: {last}')


class Site:
    def __init__(self, tmp):
        self.tmp = tmp
        self.s3, self.ddb, self.sqs = client('s3'), client('dynamodb'), client('sqs')
        self.prefix = 'desk-' + uuid.uuid4().hex[:14]
        self.q, self.d = self.prefix + '-q', self.prefix + '-d'
        self.table = self.prefix + '-table'
        outputs = deploy(client('cloudformation'), 'seed-' + self.prefix,
                         Path(__file__).parent / 'cdk_seed/seed.template.json', Prefix=self.prefix)
        eventually(lambda: self.ddb.describe_table(TableName=self.table)['Table']['TableStatus'] == 'ACTIVE')
        self.urls = {role: outputs[role] for role in ('SCAN', 'DELETE', 'EVENTS')}
        self.relay = Relay(DIRECT, self.d)
        self.env = {**os.environ, 'AWS_ENDPOINT_URL': self.relay.endpoint,
                    'AWS_DEFAULT_REGION': 'us-east-1', 'AWS_ACCESS_KEY_ID': 'test',
                    'AWS_SECRET_ACCESS_KEY': 'test', 'QUARANTINE_BUCKET': self.q,
                    'DOWNLOAD_BUCKET': self.d, 'ATTACHMENT_TABLE': self.table,
                    'PYTHONPATH': '/app', 'PYTHONDONTWRITEBYTECODE': '1',
                    **{role + '_QUEUE_URL': url for role, url in self.urls.items()}}
        self.processes = []
        self.logs = []
        self.keys = []

    def spawn(self, *args):
        directory = tempfile.mkdtemp(dir=self.tmp)
        log = open(Path(directory) / 'worker.log', 'w+')
        process = subprocess.Popen([sys.executable, '/app/worker.py', *args],
                                   cwd=directory, env=self.env, stdout=log, stderr=log)
        self.processes.append(process)
        self.logs.append(log)
        return process

    def run(self, *args):
        process = self.spawn(*args)
        assert process.wait(timeout=30) == 0, f'command failed: {args}'

    def allocate(self, size=1048576):
        key = 'account/' + uuid.uuid4().hex + '.dat'
        self.keys.append(key)
        self.run('allocate', '--key', key)
        item = self.item(key)
        assert item and item['state']['S'] == 'pending'
        assert int(item['expiresAt']['N']) >= int(time.time()) + 86390
        assert not self.exists(key)
        data = os.urandom(size)
        self.s3.put_object(Bucket=self.q, Key=key, Body=data)
        return key, data

    def item(self, key):
        return self.ddb.get_item(TableName=self.table,
            Key={'path': {'S': self.d + '/' + key}}, ConsistentRead=True).get('Item')

    def exists(self, key, bucket=None):
        return any(x['Key'] == key for x in self.s3.list_objects_v2(
            Bucket=bucket or self.d, Prefix=key).get('Contents', []))

    def uploads(self, key):
        return [u for u in self.s3.list_multipart_uploads(Bucket=self.d, Prefix=key).get('Uploads', [])
                if u['Key'] == key]

    def send(self, role, key, **extra):
        self.sqs.send_message(QueueUrl=self.urls[role],
            MessageBody=json.dumps({'key': key, **extra}), DelaySeconds=2 if role == 'DELETE' else 0)

    def scan(self, key, result='clean'):
        self.send('SCAN', key, result=result)

    def deleted(self, key):
        for message in self.sqs.receive_message(QueueUrl=self.urls['EVENTS'],
                MaxNumberOfMessages=10, WaitTimeSeconds=0).get('Messages', []):
            body = json.loads(message['Body'])
            self.sqs.delete_message(QueueUrl=self.urls['EVENTS'], ReceiptHandle=message['ReceiptHandle'])
            if body.get('event') == 'deleted' and body.get('key') == key:
                return True
        return False

    def ready(self, key):
        item = self.item(key)
        return item and item.get('state') == {'S': 'ready'} and 'expiresAt' not in item and self.exists(key)

    def leftover(self, key, gate):
        # A consumer that gave up on a withheld creation reply never learned that UploadId;
        # OPERATIONS.md leaves such an unresolved outcome to the bucket lifecycle policy.
        return [u for u in self.uploads(key) if u['UploadId'] != gate.unanswered]

    def assert_ready(self, key, data, gate=None):
        eventually(lambda: self.ready(key) and not self.exists(key, self.q),
                   message='wanted attachment did not become ready')
        assert self.s3.get_object(Bucket=self.d, Key=key)['Body'].read() == data
        assert not (self.leftover(key, gate) if gate else self.uploads(key))

    def assert_released(self, key):
        # Removal may drop the row or keep it as a finished deleting marker; it is never ready or pending.
        item = self.item(key)
        assert item is None or item.get('state') == {'S': 'deleting'}, item

    def assert_absent(self, key):
        assert not self.exists(key), 'removed attachment was published'
        assert not self.uploads(key), 'unfinished transfer survived removal'

    def close(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        self.relay.close()
        print('WIRE', json.dumps(self.relay.log, default=str))
        for log in self.logs:
            log.seek(0)
            content = log.read()
            if content:
                print('WORKER', content)
            log.close()


@pytest.fixture
def site(tmp_path):
    s = Site(tmp_path)
    try:
        yield s
    finally:
        s.close()


def test_01(site):
    s = site
    key, data = s.allocate()
    gate = s.relay.arm(key, 'stall')
    for _ in range(3): s.spawn('promote-worker')
    s.spawn('delete-worker')
    s.scan(key)
    assert gate.arrived.wait(20), 'scan did not attempt publication'
    registered = s.item(key).get('uploadId')
    s.scan(key)
    time.sleep(1)
    s.send('DELETE', key)
    eventually(lambda: s.deleted(key), seconds=17, message='removal waited for stalled promotion')
    s.assert_absent(key)
    if registered:
        assert all(u['UploadId'] != registered['S'] for u in s.uploads(key))
    s.s3.put_object(Bucket=s.q, Key=key, Body=data)
    gate.release.set()
    eventually(lambda: any(e['key'] == key and e['op'] in ('complete', 'publish')
                           and e['status'] is not None for e in s.relay.log))
    until = time.monotonic() + 2.5
    while time.monotonic() < until:
        assert not s.exists(key), 'download reappeared while publication response was withheld'
        time.sleep(.1)
    time.sleep(3)
    s.assert_absent(key)
    completions = [e for e in s.relay.log if e['key'] == key and e['op'] == 'complete' and e['forwarded']]
    for entry in completions:
        assert entry['status'] == 404 and 'NoSuchUpload' in entry.get('response', ''), entry
    assert not any(e['key'] == key and e['op'] in ('complete', 'publish') and
                   e['status'] is not None and 200 <= e['status'] < 300 for e in s.relay.log)


def test_02(site):
    s = site
    key, data = s.allocate()
    gate = s.relay.arm(key, 'commit')
    original = s.spawn('promote-worker')
    s.scan(key)
    assert gate.arrived.wait(20), 'no committed publication observed'
    assert s.exists(key), 'publication did not commit at the upstream service'
    original.kill()
    original.wait(timeout=5)
    gate.release.set()
    for _ in range(2): s.spawn('promote-worker')
    s.scan(key)
    s.scan(key)
    s.assert_ready(key, data)
    s.run('cleanup', '--now', str(int(time.time()) + 86401))
    s.assert_ready(key, data)
    creates = [e for e in s.relay.log if e['key'] == key and e['op'] == 'create' and e['forwarded']]
    # A committed transfer must be resumed, not opened again by a duplicate consumer.
    assert len(creates) == 1, creates
    completes = [e for e in s.relay.log if e['key'] == key and e['op'] == 'complete' and
                 e['status'] is not None and 200 <= e['status'] < 300]
    assert len(completes) == 1


def test_03(site):
    s = site
    key, data = s.allocate()
    gate = s.relay.arm(key, 'stall', fail_abort=True)
    s.spawn('promote-worker')
    s.spawn('delete-worker')
    s.scan(key)
    assert gate.arrived.wait(20)
    s.send('DELETE', key)
    assert gate.failed.wait(17), 'removal did not revoke its unfinished storage transfer'
    item = s.item(key)
    assert item and item['state'] == {'S': 'deleting'}, item
    assert item.get('uploadId'), 'recovery lost the outstanding transfer identity'
    assert int(item['expiresAt']['N']) <= int(time.time())
    assert not s.deleted(key), 'success announced before storage revocation finished'
    gate.failure_release.set()
    # A queue retry or the daily sweep can finish the same durable cancellation.
    time.sleep(.5)
    s.run('cleanup', '--now', str(int(time.time()) + 86401))
    eventually(lambda: s.deleted(key), seconds=17)
    s.assert_absent(key)
    s.assert_released(key)
    s.s3.put_object(Bucket=s.q, Key=key, Body=data)
    gate.release.set()
    time.sleep(5)
    s.assert_absent(key)


def test_04(site):
    s = site
    key, _ = s.allocate()
    gate = s.relay.arm(key, 'create')
    s.spawn('promote-worker')
    s.spawn('delete-worker')
    s.scan(key)
    # Ordinary copies have no create phase; still score this variant, never hang the suite.
    assert gate.arrived.wait(20), 'no unfinished transfer allocation was observable'
    s.send('DELETE', key)
    eventually(lambda: s.deleted(key), seconds=17)
    assert not s.exists(key)
    gate.release.set()
    assert gate.replied.wait(10)
    eventually(lambda: not s.leftover(key, gate), message='known registration loser leaked an upload')
    time.sleep(1)
    assert not s.exists(key), 'removed attachment was published'
    assert not s.leftover(key, gate), 'unfinished transfer survived removal'


def test_05(site):
    s = site
    key, data = s.allocate(size=0)
    s.spawn('promote-worker')
    s.scan(key)
    s.assert_ready(key, data)
    s.scan(key, result='rejected')
    s.scan(key)
    time.sleep(2)
    s.assert_ready(key, data)
    abandoned, _ = s.allocate(size=1234)
    s.run('cleanup', '--now', str(int(time.time()) + 86401))
    s.assert_ready(key, data)
    s.assert_absent(abandoned)
    assert not s.exists(abandoned, s.q)
    s.assert_released(abandoned)
    s.spawn('delete-worker')
    s.send('DELETE', key)
    eventually(lambda: s.deleted(key), seconds=17)
    s.assert_absent(key)
    s.assert_released(key)
    s.s3.put_object(Bucket=s.q, Key=key, Body=data)
    s.scan(key)
    eventually(lambda: not s.exists(key, s.q))
    s.assert_absent(key)
    s.assert_released(key)


def test_06(site):
    s = site
    key, data = s.allocate(size=8192)
    stale = Rule()
    s.relay.query_rule = stale
    sweep = s.spawn('cleanup', '--now', str(int(time.time()) + 86401))
    assert stale.arrived.wait(20), 'expiry maintenance did not enumerate records'
    s.spawn('promote-worker')
    s.scan(key)
    s.assert_ready(key, data)
    stale.release.set()
    assert sweep.wait(timeout=20) == 0
    s.assert_ready(key, data)


def test_07(site):
    s = site
    key, data = s.allocate(size=32768)
    gate = s.relay.arm(key, 'commit')
    original = s.spawn('promote-worker')
    s.scan(key)
    assert gate.arrived.wait(20)
    assert s.exists(key)
    original.kill()
    original.wait(timeout=5)
    gate.release.set()
    # No duplicate scan here: expiry maintenance must reconcile a committed publish itself.
    s.run('cleanup', '--now', str(int(time.time()) + 86401))
    s.assert_ready(key, data)


def test_08(site):
    s = site
    key, data = s.allocate(size=65536)
    gate = s.relay.arm(key, 'create')
    s.spawn('promote-worker')
    s.scan(key)
    assert gate.arrived.wait(20)
    s.spawn('promote-worker')
    s.scan(key)
    eventually(lambda: s.ready(key) and not s.exists(key, s.q))
    assert s.s3.get_object(Bucket=s.d, Key=key)['Body'].read() == data
    gate.release.set()
    assert gate.replied.wait(10)
    eventually(lambda: not s.leftover(key, gate), message='competing transfer was not reclaimed')
    s.assert_ready(key, data, gate)


def test_09(site):
    s = site
    key, data = s.allocate(size=4096)
    gate = s.relay.arm(key, 'part')
    original = s.spawn('promote-worker')
    s.scan(key)
    # The consumer dies after registering its transfer and sending data, before completion.
    assert gate.arrived.wait(20), 'no transfer part observed'
    original.kill()
    original.wait(timeout=5)
    gate.release.set()
    s.spawn('promote-worker')
    s.scan(key)
    s.assert_ready(key, data)
    s.run('cleanup', '--now', str(int(time.time()) + 86401))
    s.assert_ready(key, data)
