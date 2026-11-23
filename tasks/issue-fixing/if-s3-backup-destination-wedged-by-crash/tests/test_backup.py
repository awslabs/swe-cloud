import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
import tempfile
import threading
import time
import uuid

import boto3
from botocore.config import Config
from cdk_seed.cfn_deploy import deploy
import pytest

from relay import Relay, millis


ENDPOINT = os.environ.get('VERIFIER_ENDPOINT', 'http://localhost:4566')
APP = Path(os.environ.get('VERIFIER_APP', '/app'))


def client(service='s3'):
    return boto3.client(service, endpoint_url=ENDPOINT, region_name='us-east-1',
                        aws_access_key_id='test', aws_secret_access_key='test',
                        config=Config(s3={'addressing_style': 'path'}, retries={'max_attempts': 0}))


@pytest.fixture(scope='session', autouse=True)
def environment():
    try:
        client().list_buckets()
    except Exception as exc:
        pytest.fail('environment: S3 preflight failed: ' + str(exc))


class Run:
    def __init__(self, proc, output, errors, label, expected):
        self.proc, self.output, self.errors = proc, output, errors
        self.label, self.expected = label, expected
        self.cached = None

    def result(self, timeout=8):
        if self.cached:
            return self.cached
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
            raise AssertionError('worker did not exit: ' + self.label)
        self.output.flush()
        text = Path(self.output.name).read_text()
        lines = [json.loads(line) for line in text.splitlines() if line.strip()]
        assert len(lines) == 2, (self.label, text)
        assert lines[0]['event'] == 'started', text
        result = lines[1]
        assert result['event'] == 'result', text
        assert result['backup_uuid'] == lines[0]['backup_uuid'], text
        uuid.UUID(result['backup_uuid'])
        assert self.proc.returncode == {'BACKUP_CREATED': 0, 'BACKUP_ALREADY_EXISTS': 3, 'FAILED': 1}[result['status']], text
        self.cached = result
        return result


class Case:
    def __init__(self, root):
        self.root = root
        self.bucket = 'vault-' + uuid.uuid4().hex[:18]
        self.prefix = 'jobs/' + uuid.uuid4().hex + '/'
        self.key = self.prefix + '.lock'
        self.s3 = client()
        deploy(client('cloudformation'), 'backup-' + self.bucket,
               Path(__file__).parent / 'cdk_seed/seed.template.json', BucketName=self.bucket)
        self.s3.put_object(Bucket=self.bucket, Key=self.key,
                           Body=json.dumps(dict(state='free', owner=None, epoch=0,
                                                expires_at_ms=None, manifest_key=None)).encode())
        self.events, self.relays, self.runs = [], [], []
        self.serial = threading.Lock()
        self.lease = random.choice([2100, 2600])
        self.renew = 400

    def relay(self, mode=None, pace=0, manifest_pace=0):
        relay = Relay(self.events, self.serial, ENDPOINT, mode=mode, pace=pace,
                      manifest_pace=manifest_pace)
        self.relays.append(relay)
        return relay

    def start(self, relay, count=4):
        label = uuid.uuid4().hex
        folder = self.root / label
        source = folder / 'snapshot'
        source.mkdir(parents=True)
        expected = {}
        for i in range(count):
            name = ('nested/' if i % 2 else '') + 'part_%03d' % i
            data = hashlib.sha256((label + name).encode()).digest() * random.randint(90, 150)
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            expected[name] = data
        output = (folder / 'output').open('w+')
        errors = (folder / 'stderr').open('w+')
        proc = subprocess.Popen(
            ['python3', str(APP/'worker.py'), '--dest', f's3://{self.bucket}/{self.prefix}',
             '--source', str(source), '--job-id', label, '--endpoint-url', relay.url,
             '--lease-ms', str(self.lease), '--renew-ms', str(self.renew)],
            cwd=str(folder), env={**os.environ, 'PYTHONPATH': str(APP), 'PYTHONDONTWRITEBYTECODE': '1'},
            stdout=output, stderr=errors)
        run = Run(proc, output, errors, label, expected)
        self.runs.append(run)
        return run

    def retry(self, relay, seconds=None):
        deadline = time.monotonic() + (seconds or (self.lease/1000 + 6))
        while time.monotonic() < deadline:
            run = self.start(relay)
            result = run.result(timeout=max(.1, min(8, deadline - time.monotonic())))
            if result['status'] == 'BACKUP_CREATED':
                return run
            assert result['status'] == 'BACKUP_ALREADY_EXISTS', result
            time.sleep(.16)
        raise AssertionError('recovery deadline exceeded')

    def get_record(self):
        result = self.s3.get_object(Bucket=self.bucket, Key=self.key)
        return json.loads(result['Body'].read())

    def published(self, winner):
        result = winner.result()
        assert result['status'] == 'BACKUP_CREATED', result
        record = self.get_record()
        assert record['state'] == 'committed', record
        assert record['owner'] == result['backup_uuid'], record
        manifest = json.loads(self.s3.get_object(Bucket=self.bucket, Key=record['manifest_key'])['Body'].read())
        assert manifest['backup_uuid'] == record['owner'], manifest
        assert manifest['job_id'] == winner.label, manifest
        entries = manifest['entries']
        assert len(entries) == len(winner.expected), entries
        assert {e['name'] for e in entries} == set(winner.expected), entries
        for entry in entries:
            expected = winner.expected[entry['name']]
            assert entry['size'] == len(expected)
            assert entry['sha256'] == hashlib.sha256(expected).hexdigest()
            data = self.s3.get_object(Bucket=self.bucket, Key=entry['key'])['Body'].read()
            assert data == expected, 'published bytes changed for ' + entry['name']
        successes = [r for r in self.runs if r.proc.returncode is not None and r.proc.returncode >= 0
                     and r.result()['status'] == 'BACKUP_CREATED']
        assert successes == [winner], [r.result() for r in successes]
        self.history()
        assert not client('dynamodb').list_tables()['TableNames'], 'unexpected coordination table'
        assert not client('sqs').list_queues().get('QueueUrls'), 'unexpected coordination queue'

    def history(self):
        previous = dict(state='free', owner=None, epoch=0)
        writes = 0
        for item in self.events:
            if item['key'] != self.key:
                continue
            assert item['method'] != 'DELETE', 'catalog record removed'
            if item['method'] == 'GET':
                assert item['status'] == 200, item
            if item['method'] != 'PUT' or item['status'] != 200:
                continue
            writes += 1
            body = item['body']
            assert body and body['state'] in ('held', 'committed'), item
            assert previous['state'] != 'committed' or body == previous, 'publication replaced'
            if body['owner'] != previous['owner']:
                assert body['state'] == 'held', item
                if previous['state'] == 'held':
                    assert item['sent_ms'] >= previous['expires_at_ms'] - 150, 'unexpired owner replaced'
                assert body['epoch'] == previous['epoch'] + 1, item
            else:
                assert body['epoch'] == previous['epoch'], item
            if body['state'] == 'held':
                # A body was prepared before it entered the relay. Permit process
                # scheduling slack, but not an effective lease substitute.
                duration = body['expires_at_ms'] - item['received_ms']
                assert self.lease - 600 <= duration <= self.lease + 600, item
            previous = body
        assert writes >= 2, 'worker bypassed its endpoint'
        final = self.get_record()
        assert previous == final, 'catalog changed outside the supplied endpoint'

    def close(self):
        for relay in self.relays:
            relay.release.set()
        for run in self.runs:
            if run.proc.poll() is None:
                run.proc.kill()
            run.proc.wait()
            run.output.close()
            run.errors.close()
        for relay in self.relays:
            relay.close()
        log_root = Path(os.environ.get('VERIFIER_LOGS', '/logs/verifier'))
        log_root.mkdir(parents=True, exist_ok=True)
        (log_root/(self.bucket + '.json')).write_text(json.dumps(self.events, indent=2))
        for run in self.runs:
            (log_root/(self.bucket + '-' + run.label + '.txt')).write_text(Path(run.output.name).read_text() + '\n[stderr]\n' + Path(run.errors.name).read_text())


@pytest.fixture
def case(tmp_path):
    c = Case(tmp_path)
    try:
        yield c
    finally:
        c.close()


@pytest.mark.parametrize('position', ['publish', 'upload'])
def test_delayed_traffic(case, position):
    a_relay = case.relay(position)
    a = case.start(a_relay, count=7)
    assert a_relay.triggered.wait(12), 'worker did not reach storage boundary'
    c_relay = case.relay()
    try:
        c = case.retry(c_relay)
    finally:
        a_relay.release.set()
    assert a.result()['status'] == 'FAILED', a.result()
    case.published(c)


def test_delayed_admission(case):
    relay = case.relay('claim')
    a = case.start(relay)
    assert relay.triggered.wait(12), 'worker never tried admission'
    try:
        c = case.start(case.relay())
        assert c.result()['status'] == 'BACKUP_CREATED', c.result()
    finally:
        relay.release.set()
    assert a.result()['status'] in ('BACKUP_ALREADY_EXISTS', 'FAILED'), a.result()
    case.published(c)


def test_process_death(case):
    relay = case.relay('upload')
    a = case.start(relay, count=7)
    assert relay.triggered.wait(12), 'worker did not reach data storage'
    assert any(e['key'] == case.key and e['method'] == 'PUT' and e['status'] == 200
               for e in case.events), 'no durable reservation before death'
    a.proc.kill()
    a.proc.wait()
    relay.release.set()
    c = case.retry(case.relay())
    case.published(c)


def test_healthy_owner(case):
    # Keep uploads moving for well over two lease durations.
    a = case.start(case.relay(pace=.24), count=30)
    deadline = time.monotonic() + 14
    while time.monotonic() < deadline and case.get_record()['state'] == 'free':
        time.sleep(.03)
    assert case.get_record()['state'] == 'held', 'worker never reserved destination'
    rival_relay = case.relay()
    rivals = []
    while a.proc.poll() is None and time.monotonic() < deadline:
        rival = case.start(rival_relay)
        rivals.append(rival)
        assert rival.result()['status'] == 'BACKUP_ALREADY_EXISTS', rival.result()
        time.sleep(.15)
    assert rivals
    assert a.result()['status'] == 'BACKUP_CREATED', a.result()
    again = case.start(rival_relay)
    assert again.result()['status'] == 'BACKUP_ALREADY_EXISTS', again.result()
    case.published(a)


def test_healthy_owner_slow_manifest(case):
    # The manifest is an upload too: one slow manifest PUT, longer than the lease,
    # with catalog requests still answered promptly.
    case.lease = 2100
    a = case.start(case.relay(manifest_pace=case.lease/1000 + 2.4))
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and case.get_record()['state'] == 'free':
        time.sleep(.03)
    assert case.get_record()['state'] != 'free', 'worker never reserved destination'
    rival_relay = case.relay()
    rivals = []
    while a.proc.poll() is None and time.monotonic() < deadline:
        rival = case.start(rival_relay)
        rivals.append(rival)
        assert rival.result()['status'] == 'BACKUP_ALREADY_EXISTS', rival.result()
        time.sleep(.15)
    assert rivals
    assert a.result()['status'] == 'BACKUP_CREATED', a.result()
    case.published(a)


@pytest.mark.parametrize('position', ['drop_publish', 'drop_claim'])
def test_applied_write_without_response(case, position):
    relay = case.relay(position)
    a = case.start(relay)
    result = a.result(timeout=25)
    assert relay.triggered.is_set(), 'fault did not reach worker'
    if position == 'drop_claim' and result['status'] == 'FAILED':
        # Giving up on an ambiguous reservation is acceptable if it does not
        # wedge the destination for a replacement.
        case.published(case.retry(case.relay()))
        return
    assert result['status'] == 'BACKUP_CREATED', result
    case.published(a)


def test_simultaneous_admission(case):
    relay_a, relay_b = case.relay(pace=.1), case.relay(pace=.1)
    a, b = case.start(relay_a, count=8), case.start(relay_b, count=8)
    results = [r.result() for r in (a, b)]
    winners = [r for r in (a, b) if r.result()['status'] == 'BACKUP_CREATED']
    assert len(winners) == 1, results
    loser = b if winners[0] is a else a
    assert loser.result()['status'] == 'BACKUP_ALREADY_EXISTS', results
    case.published(winners[0])
