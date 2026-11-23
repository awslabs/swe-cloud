import hashlib
import json
import os
from pathlib import Path
import random
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
import pytest

from cdk_seed.cfn_deploy import deploy

APP = Path(os.environ.get('DISPATCH_APP_ROOT', '/app'))
SUPPORT = Path(__file__).parent / 'support'
ENDPOINT = os.environ.get('DISPATCH_TEST_ENDPOINT', 'http://localhost:4566')
DEADLINE = 10


def aws(service):
    return boto3.client(service, endpoint_url=ENDPOINT, region_name='us-east-1',
                        aws_access_key_id='test', aws_secret_access_key='test',
                        config=Config(connect_timeout=2, read_timeout=5,
                                      retries={'max_attempts': 2}))


def wait_for(function, timeout=15):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        value = function()
        if value:
            return value
        time.sleep(.15)
    return None


class Rig:
    def __init__(self, count=2):
        self.tag = 'd' + uuid.uuid4().hex[:12]
        self.root = Path(tempfile.mkdtemp(prefix=self.tag))
        self.s3, self.sqs, self.sns, self.ddb = [aws(s) for s in ('s3', 'sqs', 'sns', 'dynamodb')]
        try:
            self.s3.list_buckets()
            self.ddb.list_tables()
        except Exception as exc:
            pytest.fail('environment: service preflight failed: ' + str(exc))
        self.bucket = self.tag + '-bodies'
        cfn = aws('cloudformation')
        outputs = deploy(cfn, self.tag + '-seed',
                         Path(__file__).parent / 'cdk_seed/seed.template.json', Tag=self.tag)
        topic = outputs['TopicArn']
        self.names = ['projection-' + uuid.uuid4().hex[:6] for _ in range(count)]
        self.queues = {}
        for name in self.names + ['observer', 'error']:
            outputs = deploy(cfn, self.tag + '-' + name + '-seed',
                             Path(__file__).parent / 'cdk_seed/queue.template.json',
                             QueueName=self.tag + '-' + name, TopicArn=topic,
                             Subscribe=str(name != 'error').lower())
            queue = outputs['QueueUrl']
            self.queues[name] = queue
            if name != 'error':
                arn = self.sqs.get_queue_attributes(QueueUrl=queue,
                                                    AttributeNames=['QueueArn'])['Attributes']['QueueArn']
                policy = {'Version': '2012-10-17', 'Statement': [{
                    'Effect': 'Allow', 'Principal': {'Service': 'sns.amazonaws.com'},
                    'Action': 'sqs:SendMessage', 'Resource': arn,
                    'Condition': {'ArnEquals': {'aws:SourceArn': topic}}}]}
                self.sqs.set_queue_attributes(QueueUrl=queue, Attributes={'Policy': json.dumps(policy)})
        self.receipt_table, self.control_table = self.tag + '_receipts', self.tag + '_control'
        for name in (self.receipt_table, self.control_table):
            self.ddb.get_waiter('table_exists').wait(TableName=name)
        self.config = {
            'endpoint_url': ENDPOINT, 'region': 'us-east-1', 'topic_arn': topic,
            'bucket': self.bucket, 'body_prefix': 'bodies/',
            'subscribers': {n: self.queues[n] for n in self.names},
            'error_queue_url': self.queues['error'], 'state_prefix': self.tag + '_',
            'handler_attempts': 3, 'handler_module': 'dispatch_handler',
            'receipt_table': self.receipt_table, 'control_table': self.control_table}
        self.config_path = self.root / 'config.json'
        self.config_path.write_text(json.dumps(self.config))
        self.env = {**os.environ, 'FANOUT_CONFIG': str(self.config_path),
                    'PYTHONPATH': str(SUPPORT) + ':' + str(APP),
                    'PYTHONDONTWRITEBYTECODE': '1'}
        self.processes = {}
        self.all_processes = []
        self.expected = {}
        self.observed = {}
        self.samples = []
        self.sample_errors = []
        self.announcements = []
        self.rules = []
        self.stopped = threading.Event()
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket_path = str(self.root / 'flow.sock')
        self.listener.bind(self.socket_path)
        self.listener.listen(32)
        self.listener.settimeout(.25)
        self.env['SEAM_SOCKET'] = self.socket_path
        self.gates = threading.Thread(target=self.serve, daemon=True)
        self.gates.start()
        self.sampler = threading.Thread(target=self.sample, daemon=True)
        self.sampler.start()
        self.command('setup')
        self.command('setup')

    def command(self, *args):
        cwd = tempfile.mkdtemp(dir=self.root)
        result = subprocess.run([sys.executable, str(APP / 'fanout.py'), *args],
                                env=self.env, cwd=cwd, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, (args, result.stdout, result.stderr)
        return result

    def start(self, name):
        cwd = Path(tempfile.mkdtemp(dir=self.root))
        log = cwd / 'worker.log'
        with log.open('w') as stream:
            process = subprocess.Popen([sys.executable, str(APP / 'fanout.py'), 'subscriber', '--name', name],
                                        env=self.env, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT)
        self.processes[name] = process
        self.all_processes.append((name, process, log))
        assert wait_for(lambda: 'READY ' + name in log.read_text(), 15), log.read_text()
        return process

    def stop(self, name):
        process = self.processes.get(name)
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(4)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(3)

    def serve(self):
        while not self.stopped.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            # A paused publisher must not prevent subscriber announcements.
            threading.Thread(target=self.announce, args=(connection,), daemon=True).start()

    def announce(self, connection):
        with connection:
            connection.settimeout(3)
            try:
                data = b''
                while b'\n' not in data:
                    chunk = connection.recv(4096)
                    if not chunk:
                        raise ConnectionError('process closed announcement')
                    data += chunk
                    if len(data) > 16384:
                        raise ValueError('oversized process announcement')
                fact = json.loads(data.split(b'\n')[0])
                self.announcements.append((time.time(), fact))
                for rule in self.rules:
                    if not rule['hit'] and all(fact.get(k) == rule[k] for k in ('seam', 'subscriber', 'event_id')):
                        rule['hit'] = True
                        if rule.get('hold'):
                            while not rule.get('release') and not self.stopped.wait(.1):
                                pass
                            if not rule.get('cancel') and not self.stopped.is_set():
                                connection.sendall(b'go')
                        else:
                            process = self.processes[rule['subscriber']]
                            process.kill()
                            process.wait(3)
                        return
                connection.sendall(b'go')
            except Exception as exc:
                if not self.stopped.is_set():
                    self.sample_errors.append('process instrumentation: ' + repr(exc))

    def sample(self):
        while not self.stopped.is_set():
            try:
                keys = {o['Key'] for o in self.s3.list_objects_v2(Bucket=self.bucket, Prefix='bodies/').get('Contents', [])}
                self.samples.append((time.time(), keys))
            except Exception as exc:
                self.sample_errors.append('S3 sampling: ' + repr(exc))
            self.stopped.wait(.2)

    def bodyfile(self, event_id=None, body=None):
        event_id = event_id or 'evt-' + uuid.uuid4().hex[:14]
        body = body if body is not None else os.urandom(random.randint(280000, 370000))
        filename = self.root / (event_id + '.bin')
        filename.write_bytes(body)
        self.expected[event_id] = hashlib.sha256(body).hexdigest()
        return event_id, filename

    def publish(self):
        event_id, filename = self.bodyfile()
        self.command('publish', '--event-id', event_id, '--body-file', str(filename))
        return event_id

    def begin_publish(self, event_id, filename):
        cwd = Path(tempfile.mkdtemp(dir=self.root))
        log = cwd / 'publisher.log'
        with log.open('w') as stream:
            process = subprocess.Popen([sys.executable, str(APP / 'fanout.py'), 'publish',
                                        '--event-id', event_id, '--body-file', str(filename)],
                                       env=self.env, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT)
        self.processes['publisher'] = process
        self.all_processes.append(('publisher', process, log))
        return process

    def receipts(self, event_id, name=None):
        items = []
        request = {'TableName': self.receipt_table, 'ConsistentRead': True}
        while True:
            response = self.ddb.scan(**request)
            items.extend(response.get('Items', []))
            if 'LastEvaluatedKey' not in response:
                break
            request['ExclusiveStartKey'] = response['LastEvaluatedKey']
        return [i for i in items if i['event_id']['S'] == event_id
                and (name is None or i['subscriber']['S'] == name)]

    def complete(self, event_id):
        return {i['subscriber']['S'] for i in self.receipts(event_id)} == set(self.names)

    def exists(self, event_id):
        try:
            self.s3.head_object(Bucket=self.bucket, Key='bodies/' + event_id)
            return True
        except ClientError as exc:
            if exc.response['Error']['Code'] in ('404', 'NoSuchKey', 'NotFound'):
                return False
            raise

    def take(self, name, timeout=12):
        def receive():
            return self.sqs.receive_message(QueueUrl=self.queues[name], MaxNumberOfMessages=1,
                                             VisibilityTimeout=60, MessageAttributeNames=['All'],
                                             AttributeNames=['All']).get('Messages')
        messages = wait_for(receive, timeout)
        return messages[0] if messages else None

    def empty(self, name):
        if name == 'observer':
            # Monitoring does not own completion. Ambiguous SNS sends may
            # create more than one valid observation of the same publication.
            result = self.sqs.receive_message(QueueUrl=self.queues[name], MaxNumberOfMessages=10)
            for message in result.get('Messages', []):
                self.observe(message)
        attributes = self.sqs.get_queue_attributes(QueueUrl=self.queues[name],
                                                    AttributeNames=['All'])['Attributes']
        return sum(int(attributes.get(k, 0)) for k in
                   ('ApproximateNumberOfMessages', 'ApproximateNumberOfMessagesNotVisible',
                    'ApproximateNumberOfMessagesDelayed')) == 0

    def only_in_flight(self, name):
        attributes = self.sqs.get_queue_attributes(QueueUrl=self.queues[name],
                                                    AttributeNames=['All'])['Attributes']
        return (int(attributes.get('ApproximateNumberOfMessages', 0)) == 0
                and int(attributes.get('ApproximateNumberOfMessagesDelayed', 0)) == 0)

    def observe(self, message):
        payload = json.loads(message['Body'])['Message']
        assert len(payload.encode()) <= 2048, 'pointer exceeded transport envelope budget'
        pointer = json.loads(payload)
        event_id = pointer['event_id']
        assert pointer['body_key'] == 'bodies/' + event_id
        assert pointer['sha256'] == self.expected[event_id]
        self.sqs.delete_message(QueueUrl=self.queues['observer'],
                                ReceiptHandle=message['ReceiptHandle'])
        self.observed.setdefault(event_id, message)

    def validate_event(self, event_id):
        errors = []
        receipts = self.receipts(event_id)
        for name in self.names:
            rows = [r for r in receipts if r['subscriber']['S'] == name]
            if not rows:
                errors.append(f'{event_id}: no application receipt from {name}')
            elif any(r['digest']['S'] != self.expected[event_id] for r in rows):
                errors.append(f'{event_id}: incorrect application bytes for {name}')
        if len({r['subscriber']['S'] for r in receipts}) == len(self.names):
            last = max(float(r['observed_at']['N']) for r in receipts)
            # Historical samples detect missed deadlines even if a late cleanup eventually works.
            overdue = [(t, keys) for t, keys in self.samples if t > last + DEADLINE + .5
                       and 'bodies/' + event_id in keys]
            if overdue:
                errors.append(f'{event_id}: body persisted beyond last handler receipt + 10s')
        if self.exists(event_id):
            errors.append(f'{event_id}: body was not reclaimed')
        return errors

    def validate_storage(self):
        expected_keys = {'bodies/' + eid for eid in self.expected}
        errors = list(self.sample_errors)
        for _, keys in self.samples:
            if keys - expected_keys:
                errors.append('unexpected S3 body copies: ' + repr(keys - expected_keys))
                break
        return errors

    def close(self):
        for name in list(self.processes):
            self.stop(name)
        self.stopped.set()
        self.listener.close()
        self.gates.join(4)
        self.sampler.join(4)
        destination = Path(os.environ.get('DISPATCH_LOG_ROOT', '/logs/verifier')) / self.tag
        destination.mkdir(parents=True, exist_ok=True)
        for index, (name, _, log) in enumerate(self.all_processes):
            (destination / f'{index}-{name}.log').write_text(log.read_text())
        (destination / 'observations.json').write_text(json.dumps({
            'samples': [(t, sorted(keys)) for t, keys in self.samples],
            'announcements': self.announcements, 'errors': self.sample_errors}))


@pytest.fixture
def rig():
    instance = Rig(random.choice([2, 3]))
    try:
        yield instance
    finally:
        instance.close()


def envelope(rig, event_id):
    # Standard queues and uncertain SNS sends do not promise one observation
    # per event, or publication order. Validate every copy, retaining observations
    # for other events rather than treating a valid duplicate as the next event.
    until = time.monotonic() + 12
    while event_id not in rig.observed:
        remaining = until - time.monotonic()
        assert remaining > 0, 'SNS observer did not receive publication: ' + event_id
        copy = rig.take('observer', remaining)
        assert copy, 'SNS observer did not receive publication: ' + event_id
        rig.observe(copy)
    return rig.observed[event_id]


def test_delivery_ownership(rig):
    fast, *late = rig.names
    eid = rig.publish()
    envelope(rig, eid)
    notification = rig.take(fast)
    assert notification, 'initial projection subscription did not receive publication'
    rig.sqs.change_message_visibility(QueueUrl=rig.queues[fast],
                                      ReceiptHandle=notification['ReceiptHandle'], VisibilityTimeout=0)
    rig.start(fast)
    errors = []
    assert wait_for(lambda: rig.receipts(eid, fast)), 'initial projection never handled event'
    for _ in range(random.randint(2, 4)):
        rig.sqs.send_message(QueueUrl=rig.queues[fast], MessageBody=notification['Body'])
    time.sleep(2)
    if not rig.exists(eid):
        errors.append('shared body disappeared while other projections had not started')
    for name in late:
        rig.start(name)
    wait_for(lambda: rig.complete(eid), 15)
    wait_for(lambda: not rig.exists(eid), DEADLINE + 2)
    # A late duplicate of an already completed projection must not become poison.
    rig.sqs.send_message(QueueUrl=rig.queues[fast], MessageBody=notification['Body'])
    wait_for(lambda: rig.empty(fast), 5)
    errors += rig.validate_event(eid) + rig.validate_storage()
    for name in rig.names + ['error', 'observer']:
        if not wait_for(lambda n=name: rig.empty(n), 3):
            errors.append('undrained queue: ' + name)
    assert not errors, '\n'.join(errors)


def test_operator_backlog(rig):
    blocked = random.choice(rig.names)
    for name in rig.names:
        if name != blocked:
            rig.start(name)
    # Install the application outage before publication; no transport module is imported.
    original_publish = rig.publish
    # The application fault applies to all events for this projection only when their exact ID is present.
    slow_id = 'evt-' + uuid.uuid4().hex[:14]
    rig.ddb.put_item(TableName=rig.control_table,
                     Item={'pk': {'S': blocked + '#' + slow_id}, 'blocked': {'BOOL': True}})
    body = os.urandom(330001)
    path = rig.root / 'queued-body.bin'
    path.write_bytes(body)
    rig.expected[slow_id] = hashlib.sha256(body).hexdigest()
    rig.command('publish', '--event-id', slow_id, '--body-file', str(path))
    observed = envelope(rig, slow_id)
    original = rig.take(blocked)
    assert original, 'target subscription did not receive notification'
    assert json.loads(original['Body'])['Message'] == json.loads(observed['Body'])['Message']
    rig.sqs.change_message_visibility(QueueUrl=rig.queues[blocked],
                                      ReceiptHandle=original['ReceiptHandle'], VisibilityTimeout=0)
    rig.start(blocked)
    fast_ids = []
    for _ in range(random.randint(5, 8)):
        eid = original_publish()
        envelope(rig, eid)
        fast_ids.append(eid)
        time.sleep(random.uniform(.05, .2))
    error_copy = rig.take('error', 15)
    errors = []
    assert error_copy, 'failed application event never reached the operator queue'
    attrs = error_copy.get('MessageAttributes', {})
    if attrs.get('failed_queue', {}).get('StringValue') != blocked:
        errors.append('error transfer lost original projection identity')
    if error_copy['Body'] != original['Body']:
        errors.append('error transfer changed original notification')
    arrived = time.monotonic()
    # This dwell exceeds the reclaim SLO, independently of machine speed and publish duration.
    while time.monotonic() - arrived < 26:
        if not rig.exists(slow_id):
            errors.append('operator copy lost its S3 body during error-queue dwell')
            break
        time.sleep(.4)
    # Even a broken policy proceeds to replay so the independent application observation survives.
    remaining = 26 - (time.monotonic() - arrived)
    if remaining > 0:
        time.sleep(remaining)
    rig.ddb.delete_item(TableName=rig.control_table, Key={'pk': {'S': blocked + '#' + slow_id}})
    replayed = time.time()
    rig.sqs.send_message(QueueUrl=rig.queues[blocked], MessageBody=error_copy['Body'])
    rig.sqs.delete_message(QueueUrl=rig.queues['error'], ReceiptHandle=error_copy['ReceiptHandle'])
    wait_for(lambda: all(rig.complete(e) for e in fast_ids + [slow_id]), 15)
    wait_for(lambda: all(not rig.exists(e) for e in fast_ids + [slow_id]), DEADLINE + 2)
    if not any(float(r['observed_at']['N']) >= replayed for r in rig.receipts(slow_id, blocked)):
        errors.append('replayed operator copy did not reach its original application')
    for eid in fast_ids + [slow_id]:
        errors += rig.validate_event(eid)
    errors += rig.validate_storage()
    for name in rig.names + ['error', 'observer']:
        if not wait_for(lambda n=name: rig.empty(n), 3):
            errors.append('undrained queue: ' + name)
    assert not errors, '\n'.join(errors)


@pytest.mark.parametrize('position', ['after_fetch', 'after_handler', 'after_record', 'after_ack'])
def test_worker_interruptions(rig, position):
    last = rig.names[-1]
    for name in rig.names[:-1]:
        rig.start(name)
    eid = rig.publish()
    envelope(rig, eid)
    assert wait_for(lambda: all(rig.receipts(eid, n) for n in rig.names[:-1])), 'initial workers did not handle event'
    for name in rig.names[:-1]:
        rig.stop(name)
    rule = {'seam': position, 'subscriber': last, 'event_id': eid, 'hit': False}
    rig.rules.append(rule)
    process = rig.start(last)
    assert wait_for(lambda: rule['hit'] and process.poll() is not None, 12), (
        'worker did not announce the processing position: ' + position)
    errors = []
    if position in ('after_fetch', 'after_handler') and not rig.exists(eid):
        errors.append('unfinished projection lost body after worker interruption')
    time.sleep(.5)
    rig.start(last)
    wait_for(lambda: rig.complete(eid), 15)
    wait_for(lambda: not rig.exists(eid), DEADLINE + 2)
    errors += rig.validate_event(eid) + rig.validate_storage()
    if position == 'after_fetch' and not rig.receipts(eid, last):
        errors.append('worker never retried interrupted application dispatch')
    for name in rig.names + ['error', 'observer']:
        # The killed worker's in-flight copy returns once its visibility lease runs out.
        if not wait_for(lambda n=name: rig.empty(n), 35 if name == last else 4):
            # No deadline is set for acknowledging an extra copy of completed work. A copy the
            # killed worker still holds under its own visibility lease is not a leftover, as
            # long as the projection is durably complete and nothing is waiting to be received.
            if name == last and rig.receipts(eid, last) and rig.only_in_flight(name):
                continue
            errors.append('undrained queue: ' + name)
    assert not errors, '\n'.join(errors)


@pytest.mark.parametrize('position', ['after_upload', 'after_notify'])
def test_interrupted_publication(rig, position):
    for name in rig.names:
        rig.start(name)
    eid, filename = rig.bodyfile()
    rule = {'seam': position, 'subscriber': 'publisher', 'event_id': eid, 'hit': False}
    rig.rules.append(rule)
    producer = rig.begin_publish(eid, filename)
    assert wait_for(lambda: rule['hit'] and producer.poll() is not None, 12), (
        'publisher did not announce ' + position)
    # No producer, input file, or shared local working directory remains.
    filename.unlink()
    assert wait_for(lambda: rig.complete(eid), 15), 'uploaded publication was stranded by producer death'
    envelope(rig, eid)
    wait_for(lambda: not rig.exists(eid), DEADLINE + 2)
    errors = rig.validate_event(eid) + rig.validate_storage()
    for name in rig.names + ['error', 'observer']:
        if not wait_for(lambda n=name: rig.empty(n), 5):
            errors.append('undrained queue after publication recovery: ' + name)
    assert not errors, '\n'.join(errors)


def test_retry_after_failed_upload(rig):
    for name in rig.names:
        rig.start(name)
    eid, filename = rig.bodyfile()
    # The first attempt fails at its body upload; the producer then retries the same ID and bytes.
    # The SDK rejects this bucket name itself, so the upload fails the same way on any endpoint.
    broken = rig.root / 'broken-bucket.json'
    broken.write_text(json.dumps({**rig.config, 'bucket': rig.tag + ' unavailable'}))
    failed = subprocess.run([sys.executable, str(APP / 'fanout.py'), 'publish',
                             '--event-id', eid, '--body-file', str(filename)],
                            env={**rig.env, 'FANOUT_CONFIG': str(broken)},
                            cwd=tempfile.mkdtemp(dir=rig.root), capture_output=True, text=True,
                            timeout=30)
    assert failed.returncode != 0, 'publish succeeded although its body upload failed'
    rig.command('publish', '--event-id', eid, '--body-file', str(filename))
    errors = []
    if not wait_for(lambda: rig.complete(eid), 15):
        errors.append('producer retry after a failed upload did not reach every projection')
    envelope(rig, eid)
    wait_for(lambda: not rig.exists(eid), DEADLINE + 2)
    errors += rig.validate_event(eid) + rig.validate_storage()
    for name in rig.names + ['error', 'observer']:
        if not wait_for(lambda n=name: rig.empty(n), 5):
            errors.append('undrained queue after producer retry: ' + name)
    assert not errors, '\n'.join(errors)


def test_unknown_publish_result_and_completed_retry(rig):
    for name in rig.names:
        rig.start(name)
    eid, filename = rig.bodyfile()
    rule = {'seam': 'after_notify', 'subscriber': 'publisher', 'event_id': eid,
            'hit': False, 'hold': True}
    rig.rules.append(rule)
    producer = rig.begin_publish(eid, filename)
    assert wait_for(lambda: rule['hit'], 12), 'publisher did not announce SNS acceptance'
    assert wait_for(lambda: rig.complete(eid), 15), 'accepted SNS publication did not reach projections'
    wait_for(lambda: not rig.exists(eid), DEADLINE + 2)
    errors = []
    if rig.exists(eid):
        errors.append('unknown producer outcome prevented prompt reclamation')
    producer.kill()
    producer.wait(3)
    rule['cancel'] = True
    rule['release'] = True
    counts = {name: len(rig.receipts(eid, name)) for name in rig.names}
    retried = time.time()
    # An acknowledged retry must not rewrite a deleted body. Sampling continues
    # after success, including while all subscribers are stopped.
    for name in rig.names:
        rig.stop(name)
    rig.command('publish', '--event-id', eid, '--body-file', str(filename))
    time.sleep(2)
    if rig.exists(eid) or any(t >= retried and 'bodies/' + eid in keys for t, keys in rig.samples):
        errors.append('completed publish retry recreated its reclaimed body')
    for name in rig.names:
        rig.start(name)
    for name in rig.names + ['error', 'observer']:
        if not wait_for(lambda n=name: rig.empty(n), 5):
            errors.append('undrained queue after completed retry: ' + name)
    if counts != {name: len(rig.receipts(eid, name)) for name in rig.names}:
        errors.append('completed publication was applied again after producer retry')
    errors += rig.validate_event(eid) + rig.validate_storage()
    assert not errors, '\n'.join(errors)


def test_publish_identity_is_immutable(rig):
    first, *late = rig.names
    rig.start(first)
    eid, filename = rig.bodyfile()
    original = filename.read_bytes()
    rig.command('publish', '--event-id', eid, '--body-file', str(filename))
    envelope(rig, eid)
    assert wait_for(lambda: rig.receipts(eid, first)), 'first projection did not complete'
    changed = rig.root / 'conflicting-body.bin'
    changed.write_bytes(os.urandom(len(original) + 7))

    def rejected():
        result = subprocess.run([sys.executable, str(APP / 'fanout.py'), 'publish',
                                 '--event-id', eid, '--body-file', str(changed)],
                                env=rig.env, cwd=tempfile.mkdtemp(dir=rig.root),
                                capture_output=True, text=True, timeout=30)
        assert result.returncode != 0, 'event ID accepted different body bytes'

    rejected()
    current = rig.s3.get_object(Bucket=rig.bucket, Key='bodies/' + eid)['Body'].read()
    assert current == original, 'rejected producer retry overwrote a still-needed body'
    # Same-content retries before completion remain successful as well.
    rig.command('publish', '--event-id', eid, '--body-file', str(filename))
    for name in late:
        rig.start(name)
    assert wait_for(lambda: rig.complete(eid), 15), 'retry damaged pending projection ownership'
    wait_for(lambda: not rig.exists(eid), DEADLINE + 2)
    rejected()
    assert not rig.exists(eid), 'rejected reuse recreated an already reclaimed body'
    errors = rig.validate_event(eid) + rig.validate_storage()
    for name in rig.names + ['error', 'observer']:
        if not wait_for(lambda n=name: rig.empty(n), 5):
            errors.append('undrained queue after identity retry: ' + name)
    assert not errors, '\n'.join(errors)
