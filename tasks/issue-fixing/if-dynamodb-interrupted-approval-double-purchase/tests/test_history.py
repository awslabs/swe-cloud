import copy
import json
import os
import pathlib
import pwd
import random
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import boto3
import pytest
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.config import Config
from botocore.exceptions import ClientError

from account import Account, scan
from cdk_seed.cfn_deploy import deploy

APP = os.environ.get('TASK_APP_DIR', '/app')
LOG = pathlib.Path(os.environ.get('VERIFIER_LOG_ROOT', '/logs/verifier'))
SERIALIZER = TypeSerializer()
DESERIALIZER = TypeDeserializer()


def encode(row):
    return {key: SERIALIZER.serialize(value) for key, value in row.items()}


def decode(row):
    return {key: DESERIALIZER.deserialize(value) for key, value in row.items()}


def client():
    return boto3.client('dynamodb', endpoint_url=os.environ.get('AWS_ENDPOINT_URL', 'http://localhost:4566'),
                        region_name='us-east-1', aws_access_key_id='test', aws_secret_access_key='test',
                        config=Config(retries={'max_attempts': 2}, connect_timeout=3, read_timeout=10))


def create(db, table, key):
    try:
        db.create_table(TableName=table, KeySchema=[{'AttributeName': key, 'KeyType': 'HASH'}],
                        AttributeDefinitions=[{'AttributeName': key, 'AttributeType': 'S'}],
                        BillingMode='PAY_PER_REQUEST')
    except ClientError as exc:
        if exc.response['Error']['Code'] != 'ResourceInUseException':
            raise
    db.get_waiter('table_exists').wait(TableName=table)


def seed_create(db, table, key):
    try:
        db.describe_table(TableName=table)
    except ClientError as exc:
        if exc.response['Error']['Code'] != 'ResourceNotFoundException':
            raise
        cfn = boto3.client('cloudformation', endpoint_url=db.meta.endpoint_url,
                           region_name=db.meta.region_name)
        deploy(cfn, 'seed-' + table.replace('_', '-'),
               pathlib.Path(__file__).parent / 'cdk_seed/seed.template.json',
               TableName=table, KeyName=key)
    db.get_waiter('table_exists').wait(TableName=table)


@pytest.fixture(scope='session')
def database():
    db = client()
    try:
        db.list_tables()
        # Exercise the actual primitives used by this task before attributing any
        # worker failure to its implementation.
        name = 'probe_' + uuid.uuid4().hex
        create(db, name, 'id')
        db.put_item(TableName=name, Item={'id': {'S': 'row'}, 'v': {'N': '0'}})

        def contend():
            try:
                client().update_item(TableName=name, Key={'id': {'S': 'row'}},
                                     UpdateExpression='SET v=:one', ConditionExpression='v=:zero',
                                     ExpressionAttributeValues={':one': {'N': '1'}, ':zero': {'N': '0'}})
                return True
            except ClientError as exc:
                if exc.response['Error']['Code'] == 'ConditionalCheckFailedException':
                    return False
                raise

        with ThreadPoolExecutor(max_workers=2) as pool:
            wins = list(pool.map(lambda _: contend(), range(2)))
        assert sorted(wins) == [False, True]
        observed = scan(db, name)
        assert observed[0]['v'] == {'N': '1'}
        try:
            db.transact_write_items(TransactItems=[
                {'Put': {'TableName': name, 'Item': {'id': {'S': 'uncommitted'}}}},
                {'Update': {'TableName': name, 'Key': {'id': {'S': 'row'}},
                            'UpdateExpression': 'SET v=:two', 'ConditionExpression': 'v=:zero',
                            'ExpressionAttributeValues': {':two': {'N': '2'}, ':zero': {'N': '0'}}}}
            ])
            raise AssertionError('transaction accepted failed condition')
        except ClientError as exc:
            assert exc.response['Error']['Code'] == 'TransactionCanceledException'
        assert not db.get_item(TableName=name, Key={'id': {'S': 'uncommitted'}}, ConsistentRead=True).get('Item')
        db.delete_table(TableName=name)
    except Exception as exc:
        pytest.fail(f'environment: DynamoDB preflight failed: {exc}')
    return db


class Run:
    def __init__(self, db, account, table, url):
        self.db, self.account, self.table = db, account, table
        self.processes = []
        self.logs = []
        self.issues = []
        self.orphans = []
        self.env = {**os.environ, 'PYTHONPATH': APP, 'PYTHONDONTWRITEBYTECODE': '1',
                    'EXECUTIONS_TABLE': table, 'PROVIDER_URL': url,
                    'AWS_ACCESS_KEY_ID': 'test', 'AWS_SECRET_ACCESS_KEY': 'test',
                    'AWS_DEFAULT_REGION': 'us-east-1', 'STALE_APPROVED_THRESHOLD_S': '30', 'CLAIM_LEASE_S': '30'}

    def start(self, *args, env_extra=None):
        number = len(self.processes)
        log_path = LOG / f'worker-{number}.txt'
        handle = open(log_path, 'wb')
        directory = tempfile.mkdtemp(prefix='purchase-worker-')
        os.chmod(directory, 0o777)
        kwargs = {}
        if os.geteuid() == 0:
            user = pwd.getpwnam('nobody')
            kwargs = dict(user=user.pw_uid, group=user.pw_gid, extra_groups=[])
        process = subprocess.Popen([sys.executable, '-I', str(pathlib.Path(__file__).with_name('launch_worker.py')),
                                    f'{APP}/worker.py', *args], cwd=directory,
                                   env={**self.env, **(env_extra or {})}, start_new_session=True, stdout=handle, stderr=subprocess.STDOUT, **kwargs)
        self.processes.append(process)
        self.logs.append((handle, log_path))
        return process

    def finish(self, process, timeout=6, expect_ok=False):
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.issues.append(f'worker {process.pid} exceeded invocation deadline')
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
        if expect_ok and process.returncode != 0:
            self.issues.append(f'worker {process.pid} exited {process.returncode}')
        return process.returncode

    def once(self):
        return self.finish(self.start('sweep', '--once'), expect_ok=True)

    def quiet(self):
        if os.geteuid() != 0:
            return
        uid = pwd.getpwnam('nobody').pw_uid
        for entry in pathlib.Path('/proc').iterdir():
            if not entry.name.isdigit():
                continue
            try:
                info = (entry / 'status').read_text()
                fields = dict(line.split(':', 1) for line in info.splitlines() if ':' in line)
                if int(fields['Uid'].split()[0]) != uid or fields['State'].strip().startswith('Z'):
                    continue
                self.orphans.append(int(entry.name))
                os.kill(int(entry.name), signal.SIGKILL)
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                pass

    def snapshot(self, label):
        with self.account.lock:
            rows = {row['execution_id']: row for row in map(decode, scan(self.db, self.table))}
            self.account.audit()
            result = dict(rows=rows, records={event['record']['commitment_id']: copy.deepcopy(event['record'])
                                             for event in self.account.journal if event['kind'] == 'purchase'},
                          tamper=list(self.account.tamper), events=copy.deepcopy(self.account.events))
        (LOG / f'{label}.json').write_text(json.dumps(result, default=str, indent=2))
        return result

    def put(self, row):
        self.db.put_item(TableName=self.table, Item=encode(row))

    def wait_arrivals(self, offerings, timeout=8, kind='arrival'):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.account.lock:
                seen = {event['offering_id'] for event in self.account.events if event['kind'] == kind}
            if set(offerings) <= seen:
                return True
            time.sleep(0.05)
        self.issues.append(f'missing gateway {kind}: {set(offerings) - seen}')
        return False

    def close(self):
        for process in self.processes:
            self.finish(process, timeout=0)
        for handle, _ in self.logs:
            handle.close()


def sleep_until(target):
    while time.monotonic() < target:
        time.sleep(min(0.1, target - time.monotonic()))


def rec(kind, index=None, selected=True):
    return dict(index=index if index is not None else random.randrange(10, 10000), provider_type=kind,
                offering_id='offer-' + uuid.uuid4().hex, count=random.randint(1, 4), selected=selected)


def row(recs, status='pending'):
    return dict(execution_id='order-' + uuid.uuid4().hex, status=status,
                updated_at=int(time.time() * 1000), recommendations=recs)


@pytest.fixture(scope='session')
def incident(database):
    LOG.mkdir(parents=True, exist_ok=True)
    LOG.chmod(0o700)
    for path in pathlib.Path(APP).rglob('*'):
        if path.is_dir():
            path.chmod(path.stat().st_mode | 0o555)
        elif path.is_file():
            path.chmod(path.stat().st_mode | 0o444)
    pathlib.Path(APP).chmod(pathlib.Path(APP).stat().st_mode | 0o555)
    for table, key in [('commitments', 'commitment_ref'), ('provider_tokens', 'token_ref')]:
        seed_create(database, table, key)
        for item in scan(database, table):
            database.delete_item(TableName=table, Key={key: item[key]})
    table = 'executions_' + uuid.uuid4().hex
    seed_create(database, table, 'execution_id')
    private = tempfile.mkdtemp(prefix='account-private-')
    os.chmod(private, 0o700)
    account = Account(database, private + '/journal.jsonl')
    runner = Run(database, account, table, account.start())
    orders = {
        'a': row([rec('token')]),
        'b': row([rec('token', 23), rec('token', 91)]),
        'c': row([rec('tag_after')]),
        'd': row([rec('none')]),
        'e': row([rec('tag_after'), rec('none', selected=False)]),
        'f': row([rec('token')]),
        'g': row([rec('token', 47), rec('none', 109)]),
        'h': row([rec('none')]),
        'i': row([rec('token')], 'failed'),
    }
    orders['i'].update(error='Operator cancelled prior order', completed_at=int(time.time() * 1000))
    for order in orders.values():
        runner.put(order)
    for label in ('a', 'c', 'd'):
        account.rules['approval', orders[label]['recommendations'][0]['offering_id']] = 'commit_then_hold'
    for label in ('b', 'f'):
        account.rules['approval', orders[label]['recommendations'][0]['offering_id']] = 'hold_before_commit'
    account.rules['approval', orders['e']['recommendations'][0]['offering_id']] = 'delay_then_commit'
    account.rules['approval', orders['g']['recommendations'][1]['offering_id']] = 'commit_then_hold'
    account.rules['main', orders['f']['recommendations'][0]['offering_id']] = 'commit_then_hold'
    account.rules['reclaim', orders['f']['recommendations'][0]['offering_id']] = 'commit_then_hold'
    started = time.monotonic()
    approvals = {label: runner.start('approve', '--execution-id', order['execution_id'], '--actor', 'finance@example.org')
                 for label, order in orders.items() if label in 'abcdefg'}
    try:
        initial_offers = [orders[label]['recommendations'][0]['offering_id'] for label in 'abcdefg']
        initial_offers += [orders['g']['recommendations'][1]['offering_id']]
        runner.wait_arrivals(initial_offers)
        held_offers = [orders[label]['recommendations'][0]['offering_id'] for label in 'abcdf']
        held_offers.append(orders['g']['recommendations'][1]['offering_id'])
        runner.wait_arrivals(held_offers, kind='parked')
        admitted = runner.snapshot('admitted')
        runner.once()  # all original invocations still within the admission margin
        sleep_until(started + 6)
        for label, process in approvals.items():
            runner.finish(process, timeout=0, expect_ok=(label == 'e'))
        runner.quiet()
        first = runner.snapshot('first')
        sleep_until(started + 12)
        runner.once()
        runner.quiet()
        second = runner.snapshot('second')
        sleep_until(started + 35)
        account.phase = 'main'
        mains = [runner.start('sweep', '--once') for _ in range(2)]
        # Confirm the response-loss boundary when the implementation reaches it;
        # a missing arrival remains a graded outcome rather than aborting scoring.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if all(process.poll() is not None for process in mains):
                break
            time.sleep(0.05)
        for process in mains:
            runner.finish(process, timeout=0)
        runner.quiet()
        middle = runner.snapshot('middle')
        sleep_until(started + 75)
        account.phase = 'reclaim'
        runner.once()
        runner.quiet()
        reclaim = runner.snapshot('reclaim')
        # A resumable implementation may defer dispatch until the reclaim pass.
        # Its first recovery purchase still loses its response, and receives a
        # later invocation after that worker's ownership has expired.
        sleep_until(started + 115)
        account.phase = 'settle'
        runner.once()
        runner.quiet()
        final = runner.snapshot('final')
        result = dict(runner=runner, orders=orders, admitted=admitted, first=first, second=second, middle=middle, reclaim=reclaim, final=final,
                      issues=list(runner.issues))
        yield result
    finally:
        runner.close()
        account.server.shutdown()


def commitments(snapshot, recommendation):
    return [record for record in snapshot['records'].values() if record['offering_id'] == recommendation['offering_id']]


def actual(snapshot, order):
    return snapshot['rows'][order['execution_id']]


def assert_references(snapshot, order):
    current = actual(snapshot, order)
    for original in order['recommendations']:
        entries = [entry for entry in current['recommendations'] if entry['index'] == original['index']]
        assert len(entries) == 1, 'recommendation identity was lost'
        entry = entries[0]
        if not original['selected']:
            assert not entry.get('commitment_id') and not entry.get('purchased')
            continue
        if entry.get('commitment_id'):
            real = commitments(snapshot, original)
            assert len(real) == 1
            assert entry['commitment_id'] == real[0]['commitment_id']
        if current['status'] == 'completed':
            assert entry.get('commitment_id'), 'completed history lacks a real commitment reference'


def test_original_and_young_maintenance(incident):
    orders, first, second = (incident[key] for key in ('orders', 'first', 'second'))
    problems = []
    for label, expected in [('a', 1), ('b', 0), ('c', 1), ('d', 1), ('e', 1), ('f', 0), ('g', 1)]:
        recommendation = orders[label]['recommendations'][0]
        if len(commitments(first, recommendation)) != expected:
            problems.append(f'initial purchase count for {label}')
    # The original workers may checkpoint, reconcile, or finish after a local
    # HTTP timeout. Their own progress is not evidence of maintenance stealing.
    current = actual(first, orders['e'])
    assert current['status'] in ('completed', 'failed')
    if current['status'] == 'failed':
        assert current.get('error'), 'a lost tag_after response needs an operator explanation'
    assert_references(first, orders['e'])
    assert first['rows'] == second['rows'], 'maintenance changed rows inside the configured age margin'
    assert first['records'] == second['records'], 'maintenance bought inside the configured age margin'
    assert not problems, problems


def test_young_approval_is_unchanged_by_maintenance(incident):
    runner = incident['runner']
    order = row([rec('token')], 'approved')
    runner.put(order)
    try:
        before = runner.snapshot('young-before')
        assert runner.once() == 0
        after = runner.snapshot('young-after')
        assert before['rows'] == after['rows'], 'maintenance changed an approval inside its age margin'
        assert before['records'] == after['records'], 'maintenance bought inside the age margin'
    finally:
        # This fixture must not age into unrelated later recovery scenarios.
        runner.db.delete_item(TableName=runner.table, Key={'execution_id': {'S': order['execution_id']}})


def test_no_duplicate_financial_obligations(incident):
    final, orders = incident['final'], incident['orders']
    failures = []
    for label in 'abcdefg':
        for recommendation in orders[label]['recommendations']:
            expected = 1 if recommendation['selected'] else 0
            count = len(commitments(final, recommendation))
            if count != expected:
                failures.append(f'{label}/{recommendation["index"]}: {count} commitments, expected {expected}')
    assert not failures, failures


@pytest.mark.parametrize('label', list('abf'))
def test_automatic_completion_and_second_worker_loss(incident, label):
    final, orders = incident['final'], incident['orders']
    current = actual(final, orders[label])
    assert current['status'] == 'completed', (label, current)
    assert current.get('completed_at')
    assert_references(final, orders[label])


@pytest.mark.parametrize('label', list('cd'))
def test_ambiguous_history(incident, label):
    final, orders = incident['final'], incident['orders']
    current = actual(final, orders[label])
    assert current['status'] in ('failed', 'completed')
    if current['status'] == 'failed':
        assert current.get('error'), 'ambiguous purchases need an operator explanation'
    else:
        assert_references(final, orders[label])


def test_mixed_history(incident):
    final, orders = incident['final'], incident['orders']
    current = actual(final, orders['g'])
    assert current['status'] == 'partially_completed', current
    assert current.get('error') and current.get('completed_at')
    assert_references(final, orders['g'])
    successful = next(entry for entry in current['recommendations'] if entry['index'] == 47)
    assert successful.get('commitment_id'), 'successful sibling lost from mixed history'


def test_account_audit_and_execution_preservation(incident):
    orders = incident['orders']
    assert not incident['runner'].orphans, 'workers outlived their invocations'
    for label in ('first', 'second', 'middle', 'reclaim', 'final'):
        snapshot = incident[label]
        assert not snapshot['tamper'], snapshot['tamper']
        assert set(snapshot['rows']) == {order['execution_id'] for order in orders.values()}
        for protected in 'hi':
            assert actual(snapshot, orders[protected]) == orders[protected]
    for label in 'abcdefg':
        assert actual(incident['final'], orders[label])['status'] in ('completed', 'partially_completed', 'failed')


def test_normal_approval_contention_and_terminal_history(incident):
    runner = incident['runner']
    order = row([rec('token', 13), rec('tag_after', 101), rec('none', 301), rec('token', 499, False)], 'notified')
    runner.put(order)
    approvals = [runner.start('approve', '--execution-id', order['execution_id'], '--actor', 'buyer@example.org') for _ in range(2)]
    codes = [runner.finish(process, timeout=8) for process in approvals]
    assert sum(code == 0 for code in codes) == 1, codes
    before = runner.snapshot('ordinary-before')
    assert actual(before, order)['status'] == 'completed'
    assert_references(before, order)
    for recommendation in order['recommendations']:
        assert len(commitments(before, recommendation)) == (1 if recommendation['selected'] else 0)
    assert runner.finish(runner.start('approve', '--execution-id', order['execution_id'], '--actor', 'other@example.org')) != 0
    runner.once()
    after = runner.snapshot('ordinary-after')
    assert before['rows'] == after['rows'], 'terminal history was overwritten'
    assert before['records'] == after['records'], 'terminal approval bought again'
    assert not after['tamper']


def test_competing_recovery_preserves_winners_history(incident):
    runner = incident['runner']
    order = row([rec('token')], 'approved')
    order['updated_at'] -= 45000
    runner.put(order)
    directory = tempfile.mkdtemp(prefix='purchase-seam-')
    os.chmod(directory, 0o777)
    path = directory + '/socket'
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    os.chmod(path, 0o777)
    listener.listen()
    listener.settimeout(8)
    late = runner.start('sweep', '--once', env_extra={'SEAM_SOCKET': path})
    connection = None
    try:
        connection, _ = listener.accept()
        connection.settimeout(8)
        raw = b''
        while b'\n' not in raw:
            part = connection.recv(4096)
            assert part, 'worker closed the pause socket without announcing its position'
            raw += part
        announcement = json.loads(raw.split(b'\n')[0])
        assert announcement['seam'] == 'stage_2'
        assert announcement['execution_id'] == order['execution_id']
        # Keep the stale reader paused until the rival actually finishes. A
        # correct resumable policy can split claiming, reconciliation and
        # dispatch across maintenance invocations.
        for attempt in range(3):
            runner.once()
            before = runner.snapshot(f'competing-attempt-{attempt}')
            if actual(before, order)['status'] not in ('approved', 'running'):
                break
            if attempt < 2:
                sleep_until(time.monotonic() + float(runner.env['CLAIM_LEASE_S']) + 6)
        before = runner.snapshot('competing-before')
        assert actual(before, order)['status'] == 'completed'
        assert_references(before, order)
        connection.sendall(b'go')
        assert runner.finish(late, timeout=6) == 0
        after = runner.snapshot('competing-after')
        assert before['rows'] == after['rows'], 'a stale sweep overwrote the winning worker\'s terminal history'
        assert before['records'] == after['records']
        assert not after['tamper']
    finally:
        if connection:
            connection.close()
        listener.close()
        runner.finish(late, timeout=0)


def command_id(runner, process):
    code = runner.finish(process, expect_ok=True)
    path = runner.logs[runner.processes.index(process)][1]
    output = path.read_text().strip().splitlines()
    assert code == 0 and output, (code, path.read_text())
    return output[-1]


def assert_parent_history(snapshot, parent):
    current = actual(snapshot, parent)
    for key in ('execution_id', 'retry_of', 'status', 'error', 'completed_at', 'approved_by'):
        if key in parent:
            assert current.get(key) == parent[key], f'Retry changed parent purchase history field {key}'
    for recommendation in parent['recommendations']:
        entries = [entry for entry in current['recommendations'] if entry['index'] == recommendation['index']]
        assert len(entries) == 1, 'Retry lost parent recommendation identity'
        for key in ('index', 'provider_type', 'offering_id', 'count', 'selected', 'commitment_id', 'purchased'):
            if key in recommendation:
                assert entries[0].get(key) == recommendation[key], f'Retry changed parent purchase field {key}'


def test_retry_rejects_ineligible_parents_without_side_effects(incident):
    runner = incident['runner']
    fixtures = [row([rec('token')], 'running'), row([], 'completed'), row([rec('none')], 'pending')]
    fixtures[0]['updated_at'] -= 60000
    for order in fixtures:
        runner.put(order)
    before = runner.snapshot('retry-ineligible-before')
    try:
        for order in fixtures:
            code = runner.finish(runner.start('retry', '--execution-id', order['execution_id']))
            assert code != 0, 'Retry accepted an active, completed or pending parent'
        after = runner.snapshot('retry-ineligible-after')
        assert before['rows'] == after['rows']
        assert before['records'] == after['records']
    finally:
        # Keep this deliberately active admission fixture out of later sweeps.
        for order in fixtures:
            runner.db.delete_item(TableName=runner.table, Key={'execution_id': {'S': order['execution_id']}})


def test_retry_lineage_and_lost_acceptance_reply(incident):
    runner = incident['runner']
    # An old release published a mixed terminal outcome without retaining all
    # purchase references. The account, not the failed status, is authoritative.
    parent = row([rec('token', 31), rec('token', 82), rec('none', 203), rec('token', 405, False)],
                 'partially_completed')
    parent.update(error='Prior worker lost purchase replies', completed_at=int(time.time() * 1000))
    references = []
    import hashlib
    for recommendation in parent['recommendations'][:3]:
        body = {key: int(recommendation[key]) if key == 'count' else recommendation[key]
                for key in ('provider_type', 'offering_id', 'count')}
        if body['provider_type'] == 'token':
            token = hashlib.sha256(f'{parent["execution_id"]}:{recommendation["index"]}'.encode()).hexdigest()
            body.update(client_token=token, tags={'cudly-idempotency-token': token})
        references.append(runner.account.purchase(body)['commitment_id'])
    parent['recommendations'][0].update(commitment_id=references[0], purchased=True)
    parent['updated_at'] = parent['completed_at'] = int(time.time() * 1000)
    runner.put(parent)
    before = runner.snapshot('retry-before')
    directory = tempfile.mkdtemp(prefix='retry-seam-')
    os.chmod(directory, 0o777)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    path = directory + '/socket'
    listener.bind(path)
    os.chmod(path, 0o777)
    listener.listen()
    listener.settimeout(8)
    lost = runner.start('retry', '--execution-id', parent['execution_id'], env_extra={'SEAM_SOCKET': path})
    connection = None
    try:
        connection, _ = listener.accept()
        connection.settimeout(8)
        raw = b''
        while b'\n' not in raw:
            chunk = connection.recv(4096)
            assert chunk, 'Retry closed its position socket before announcing acceptance'
            raw += chunk
        announcement = json.loads(raw.split(b'\n')[0])
        assert announcement['seam'] == 'stage_3'
        assert announcement['execution_id'] == parent['execution_id']
        # The accepting worker loses its reply while another UI request is in
        # flight. Both live requests must refer to the same durable successor.
        concurrent = [runner.start('retry', '--execution-id', parent['execution_id']) for _ in range(2)]
        ids = [command_id(runner, process) for process in concurrent]
        assert len(set(ids + [announcement['child_id']])) == 1, 'Concurrent Retry accepted multiple children'
        child_id = ids[0]
        runner.finish(lost, timeout=0)
        runner.quiet()
        assert command_id(runner, runner.start('retry', '--execution-id', parent['execution_id'])) == child_id
        pending = runner.snapshot('retry-pending')
        assert set(pending['rows']) - set(before['rows']) == {child_id}, 'Retry left duplicate or orphan executions'
        child = pending['rows'][child_id]
        assert child['status'] == 'pending' and child['retry_of'] == parent['execution_id']
        assert pending['records'] == before['records'], 'Retry bought before approval'
        assert_parent_history(pending, parent)
        # Expire native-token retention before approval of the new review row.
        sleep_until(time.monotonic() + 21)
        runner.finish(runner.start('approve', '--execution-id', child_id, '--actor', 'finance@example.org'), expect_ok=True)
        current = runner.snapshot('retry-approved')
        for attempt in range(3):
            if current['rows'][child_id]['status'] in ('completed', 'partially_completed', 'failed'):
                break
            sleep_until(time.monotonic() + float(runner.env['CLAIM_LEASE_S']) + 6)
            runner.once()
            current = runner.snapshot(f'retry-settled-{attempt}')
        child_order = dict(parent, execution_id=child_id)
        assert current['rows'][child_id]['status'] == 'partially_completed', current['rows'][child_id]
        assert current['rows'][child_id].get('error') and current['rows'][child_id].get('completed_at')
        assert_references(current, child_order)
        for recommendation, reference in zip(parent['recommendations'][:2], references):
            entry = next(item for item in current['rows'][child_id]['recommendations']
                         if item['index'] == recommendation['index'])
            assert entry.get('commitment_id') == reference, 'Retry lost a known or recoverable successful sibling'
        assert_parent_history(current, parent)
        # A second retry generation must keep the original financial identity,
        # including the unresolved unkeyed purchase, and preserve its parent's
        # terminal purchase history as well.
        child_parent = copy.deepcopy(current['rows'][child_id])
        grandchild_id = command_id(runner, runner.start('retry', '--execution-id', child_id))
        runner.finish(runner.start('approve', '--execution-id', grandchild_id, '--actor', 'finance@example.org'), expect_ok=True)
        final = runner.snapshot('retry-final')
        for attempt in range(3):
            if final['rows'][grandchild_id]['status'] in ('completed', 'partially_completed', 'failed'):
                break
            sleep_until(time.monotonic() + float(runner.env['CLAIM_LEASE_S']) + 6)
            runner.once()
            final = runner.snapshot(f'retry-final-settled-{attempt}')
        assert final['rows'][grandchild_id]['status'] == 'partially_completed'
        assert_references(final, dict(parent, execution_id=grandchild_id))
        assert_parent_history(final, parent)
        assert_parent_history(final, child_parent)
        assert final['records'] == before['records'], 'A retry generation created an additional financial obligation'
        assert not final['tamper']
    finally:
        if connection:
            connection.close()
        listener.close()
        runner.finish(lost, timeout=0)
