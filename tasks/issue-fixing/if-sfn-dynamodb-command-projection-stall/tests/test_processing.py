import concurrent.futures
import json
import os
from pathlib import Path
import random
import threading
import time
import urllib.request
import uuid

import pytest
from harness import Rig, client, eventually


@pytest.fixture
def rig(request):
    try:
        urllib.request.urlopen('http://localhost:4566/_floci/health', timeout=5).read()
        client('stepfunctions').list_state_machines()
        client('dynamodb').list_tables()
    except Exception as exc:
        pytest.fail('environment: AWS emulator preflight failed: ' + repr(exc))
    logdir = Path('/logs/verifier') / request.node.name
    instance = Rig(logdir)
    try:
        yield instance
    finally:
        instance.close()


def entity():
    return 'account-' + uuid.uuid4().hex[:14]


def commit_time(rig, name, version, status):
    return eventually(lambda: next((t for t, k, row in rig.gateway.commits if k == (name, version) and
                                    row.get('status', {}).get('S') == status), None), description='status commit')


def token_time(rig, name, version):
    return eventually(lambda: next((t for t, k in rig.gateway.tokens.values() if k == (name, version)), None),
                      description='callback registration')


def test_01_interleaved_accounts(rig):
    """Two independent active accounts, one with a slow final status write."""
    a, b = entity(), entity()
    a_at_final, b_at_sync, b_at_finish = threading.Event(), threading.Event(), threading.Event()
    release_a, release_b_sync, release_b_finish = rig.gate(), rig.gate(), rig.gate()

    def before(key, status, body):
        if key == (a, 2) and status == 'finish:FINISHED':
            a_at_final.set()
            release_a.wait(15)
        if key == (b, 1) and status == 'sync_data:FINISHED':
            b_at_sync.set()
            release_b_sync.wait(12)
        if key == (b, 1) and status == 'finish:STARTED':
            b_at_finish.set()
            release_b_finish.wait(5)
    rig.gateway.before_write = before

    # Establish a short prefix without requiring a wait callback to succeed.
    a1 = rig.publish(a, 1)
    rig.successful(a1)
    a2 = rig.publish(a, 2)
    eventually(a_at_final.is_set, description='second version final write')
    a3 = rig.publish(a, 3)
    start_a = token_time(rig, a, 3)

    b1 = rig.publish(b, 1)
    eventually(b_at_sync.is_set, description='concurrent sync')
    b2 = rig.publish(b, 2)
    token_time(rig, b, 2)
    eventually(lambda: rig.wait_deleted(b), description='wait handler returned')
    release_b_sync.set()
    eventually(b_at_finish.is_set, description='finish admission')

    # The pending status writes are held longer than the callback budget and
    # admission guard respectively. Observe history, not current row status alone.
    rig.deadline(a3, start_a)
    time.sleep(5.1)
    release_b_finish.set()
    release_a.set()
    rig.successful(a2, a3, b1, b2)
    rig.assert_outcomes(a, [1, 2, 3])
    rig.assert_outcomes(b, [1, 2])
    rig.assert_health()


def test_02_registered_successor(rig):
    name = entity()
    hold, arrived = rig.gate(), threading.Event()
    def before(key, status, body):
        if key == (name, 1) and status == 'finish:STARTED':
            arrived.set()
            hold.wait(12)
    rig.gateway.before_write = before
    second = rig.publish(name, 2)
    token_time(rig, name, 2)
    eventually(lambda: rig.wait_deleted(name), description='registration returned')
    first = rig.publish(name, 1)
    eventually(arrived.is_set, description='finish worker')
    hold.set()
    start = commit_time(rig, name, 1, 'finish:STARTED')
    rig.deadline(second, start)
    rig.successful(first, second)
    rig.assert_outcomes(name, [1, 2])
    rig.assert_health()


def test_03_delayed_publication(rig):
    name = entity()
    first = rig.publish(name, 1)
    rig.successful(first)
    second = rig.publish(name, 2)
    rig.deadline(second, token_time(rig, name, 2))
    rig.successful(second)
    rig.assert_outcomes(name, [1, 2])
    rig.assert_health()


def test_04_rejected_sync(rig):
    name = entity()
    # Configure the managed adapter before any messages for this entity exist.
    rig.sink_process.terminate()
    rig.sink_process.wait(timeout=3)
    rig.processes.remove(rig.sink_process)
    import harness
    import sys
    env = {**rig.env, 'DRIVER_SECRET': rig.gateway.secret, 'EFFECTS_TABLE': rig.effects_table,
           'PROJECTION_TABLE': rig.projection_table, 'FAILED_ENTITY': name}
    rig.sink_process = rig.spawn([sys.executable, str(Path(harness.__file__).resolve()), 'sink'], env, 'sink-rejected')
    first = rig.publish(name, 1)
    eventually(lambda: rig.state(first) == 'ABORTED', description='rejected predecessor')
    second = rig.publish(name, 2)
    token_time(rig, name, 2)
    time.sleep(6)
    assert rig.wait_release(second) is None, 'successor was released after predecessor failed'
    assert not rig.effects(name), 'failed chain produced downstream effects'
    assert rig.state(second) == 'RUNNING'
    assert not any(e['type'] == 'TaskStateEntered' and e['stateEnteredEventDetails']['name'] == 'sync_data'
                   for e in rig.history(second))
    rig.assert_health()


@pytest.mark.parametrize('already_finished', [False, True])
def test_05_execution_redelivery(rig, already_finished):
    name = entity()
    first = None
    if already_finished:
        first = rig.publish(name, 1)
        rig.successful(first)
    registrations = []
    count_lock = threading.Lock()
    # On redelivery after completion, let the first execution act before the
    # redelivery writes. A read-back comparison after overwriting is insufficient.
    def before(key, status, body):
        token = rig.gateway.assignment(body, 'taskToken')
        if key != (name, 2) or not token:
            return
        with count_lock:
            registrations.append(token)
            number = len(registrations)
        if already_finished and number == 2:
            limit = time.monotonic() + 1.5
            while time.monotonic() < limit and registrations[0] not in rig.gateway.completed:
                time.sleep(.01)
    rig.gateway.before_write = before
    second = rig.publish(name, 2)
    duplicate = rig.publish(name, 2, duplicate=True)
    token_time(rig, name, 2)
    eventually(lambda: rig.wait_deleted(name, 2), description='both redeliveries processed')
    if not already_finished:
        first = rig.publish(name, 1)
    # Exactly one can enter sync; the other can be parked or explicitly retired.
    eventually(lambda: any(rig.state(e) == 'SUCCEEDED' for e in (second, duplicate)), description='one execution applied')
    third = rig.publish(name, 3)
    rig.successful(first, third)
    time.sleep(.4)
    parked = [e for e in (second, duplicate) if rig.state(e) == 'RUNNING']
    rig.assert_outcomes(name, [1, 2, 3], allow_parked=parked)
    rig.assert_health()


@pytest.mark.parametrize('delayed_side', ['push', 'pull'])
def test_06_overlapping_callbacks(rig, delayed_side):
    name = entity()
    entered, has_token, pulled = threading.Event(), threading.Event(), threading.Event()
    published = threading.Event()
    def before_read(key):
        # Hold only v1's finish read of the next row, made before v2 exists.
        if key == (name, 2) and not entered.is_set() and not published.is_set():
            entered.set()
            has_token.wait(2)
    def after_write(key, row, body):
        if key == (name, 2) and 'taskToken' in row:
            has_token.set()
    def before_send(body):
        output = json.loads(body['output'])
        if output.get('by') == delayed_side:
            # A losing callback must not turn finish into a failed execution.
            limit = time.monotonic() + 1.5
            while time.monotonic() < limit:
                if any(json.loads(b['output']).get('by') in ('push', 'pull') and
                       json.loads(b['output']).get('by') != delayed_side and result == 'ok'
                       for _, _, b, result in rig.gateway.callbacks):
                    pulled.set()
                    break
                time.sleep(.01)
    rig.gateway.before_read = before_read
    rig.gateway.after_write = after_write
    rig.gateway.before_send = before_send
    first = rig.publish(name, 1)
    # Alternate durable handshakes may not use a next-row read; do not require it.
    entered.wait(5)
    published.set()
    second = rig.publish(name, 2)
    token_time(rig, name, 2)
    rig.successful(first, second)
    errors = [t for t, _, _, result in rig.gateway.callbacks if result in ('TaskDoesNotExist', 'TaskTimedOut')]
    if errors:
        assert commit_time(rig, name, 1, 'finish:FINISHED') - min(errors) < 5
    rig.assert_outcomes(name, [1, 2])
    rig.assert_health()


def test_07_publication_burst(rig):
    names = [entity() for _ in range(10)]
    seed = int(uuid.uuid4().hex[:8], 16)
    (rig.logdir / 'seed.txt').write_text(str(seed))
    randomizer = random.Random(seed)
    gaps = {n: [randomizer.uniform(0, .2) for _ in range(6)] for n in names}
    for name in names:
        for version in randomizer.sample(range(1, 7), 2):
            rig.gateway.failures[(name, version)] = 1
    def publish_chain(name):
        result = []
        for version in range(1, 7):
            result.append(rig.publish(name, version))
            time.sleep(gaps[name][version - 1])
        return result
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        runs = list(pool.map(publish_chain, names))
    rig.successful(*(e for chain in runs for e in chain), timeout=45)
    for name in names:
        rig.assert_outcomes(name, list(range(1, 7)))
    rig.assert_health()


@pytest.mark.parametrize('condition', ['closed_callback', 'capacity'])
def test_08_unavailable_successor(rig, condition):
    name = entity()
    if condition == 'closed_callback':
        second = rig.publish(name, 2)
        token_time(rig, name, 2)
        eventually(lambda: rig.wait_deleted(name), description='registered wait')
        rig.sfn.stop_execution(executionArn=second, error='OperatorCancelled')
        first = rig.publish(name, 1)
        rig.successful(first)
        rig.assert_outcomes(name, [1], allow_parked=[second])
    else:
        # The accepted residual is a failed pull coinciding with a missed push.
        # This case exhausts only the push: later publication can still proceed.
        rig.gateway.failures[(name, 2)] = 3
        first = rig.publish(name, 1)
        rig.successful(first)
        second = rig.publish(name, 2)
        rig.deadline(second, token_time(rig, name, 2))
        rig.successful(second)
        rig.assert_outcomes(name, [1, 2])
    rig.assert_health()
