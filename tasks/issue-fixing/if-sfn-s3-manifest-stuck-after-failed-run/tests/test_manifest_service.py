"""
Verifier for the manifest service. Every test gets its own deployment and its
own fleet of service processes, and grades only what a client and an operator
can observe: HTTP responses, the executions of the deployment's state machine
with their statuses, and the objects in the bucket.
"""
import random
import threading
import time

import pytest

from harness import (
    Deployment,
    Fleet,
    HeldWorker,
    Manifest,
    Outcome,
    Proxy,
    QUIET_WINDOW_S,
    SFN_QUOTAS,
    aws,
    burst,
    floci_is_live,
    fresh_request,
    http_call,
    poll,
    settle,
)

BURST = 8
INSTANCES = 3

#: The account proxy of the running test: every service process reaches AWS
#: through it, so they all draw on the same Step Functions quotas
account: Proxy | None = None


@pytest.fixture(scope='session', autouse=True)
def emulator():
    if not floci_is_live():
        pytest.exit('environment: the emulator is not answering on its health endpoint', returncode=3)


@pytest.fixture
def stack(request):
    global account
    deployment = Deployment.create()
    worker = HeldWorker(deployment)
    account = Proxy(quotas=SFN_QUOTAS)
    fleets = []

    def fleet(count=INSTANCES, label=None, endpoint=None):
        f = Fleet(deployment, count, label or request.node.name,
                  extra_env={'AWS_ENDPOINT_URL': endpoint or account.url})
        fleets.append(f)
        return f

    try:
        yield deployment, worker, fleet
    finally:
        print(f'Step Functions usage through the account: {account.usage()}')
        for f in fleets:
            f.kill()
        worker.stop()
        deployment.stop_running()
        account.close()
        account = None


def summary(outcomes: list[Outcome]) -> str:
    lines = [f'  client {i}: {o.describe()}' for i, o in enumerate(outcomes)]
    if account is not None:
        lines.append(f'  Step Functions usage so far: {account.usage()}')
    return '\n'.join(lines)


def assert_one_new_attempt(deployment: Deployment, worker: HeldWorker, before: set[str],
                           outcomes: list[Outcome], what: str) -> str:
    """
    All clients hold a 301 for the same running execution, and that execution
    is the only one that appeared.
    """
    after = deployment.execution_arns()
    new = after - before
    statuses = deployment.statuses()
    assert all(o.kind == '301' for o in outcomes), (
        f'{what}: every client should end up polling one running generation attempt; got\n'
        f'{summary(outcomes)}\nnew executions: {sorted((a, statuses.get(a)) for a in new)}')
    named = {o.execution for o in outcomes}
    assert len(new) == 1, (
        f'{what}: expected exactly one new execution for {len(outcomes)} concurrent requests, '
        f'found {len(new)}: {sorted((a, statuses.get(a)) for a in new)}\n{summary(outcomes)}')
    assert named == new, (
        f'{what}: clients were sent to {sorted(named)}, but the new execution is {sorted(new)}')
    arn, = new
    assert worker.wait_for(arn) is not None, f'{what}: the task of {arn} never reached the generator'
    return arn


def fail_attempt(deployment: Deployment, worker: HeldWorker, arn: str):
    worker.release(arn, ok=False)
    status = deployment.wait_closed(arn)
    assert status == 'FAILED', f'harness: {arn} should have failed, status {status}'


def succeed_attempt(deployment: Deployment, worker: HeldWorker, arn: str):
    worker.release(arn, ok=True)
    status = deployment.wait_closed(arn)
    assert status == 'SUCCEEDED', f'harness: {arn} should have succeeded, status {status}'


def assert_quiet(deployment: Deployment, before: set[str], what: str, window: float = QUIET_WINDOW_S):
    time.sleep(window)
    new = deployment.execution_arns() - before
    assert not new, f'{what}: no client asked, yet new executions appeared: {sorted(new)}'


def assert_poll_reports_failure(deployment: Deployment, fleet: Fleet, outcome: Outcome, what: str):
    before = deployment.execution_arns()
    status, body = http_call('GET', fleet.port(1), outcome.body['Location'])
    assert status == 500, (
        f'{what}: polling the token of the failed attempt should report the failure (HTTP 500); '
        f'got HTTP {status} {body}')
    assert_quiet(deployment, before, f'{what}, after polling the failed attempt', window=1.0)


def assert_served(deployment: Deployment, outcome: Outcome, what: str):
    assert outcome.kind == '302', f'{what}: expected the manifest (302); got {outcome.describe()}'
    location = outcome.body.get('Location', '')
    prefix = f's3://{deployment.bucket}/'
    assert location.startswith(prefix), f'{what}: 302 points outside the bucket: {location!r}'
    assert deployment.object_exists(location[len(prefix):]), f'{what}: 302 points to a missing object {location!r}'


def assert_history(deployment: Deployment, expected: dict[str, int], what: str):
    counts: dict[str, int] = {}
    for e in deployment.executions():
        counts[e['status']] = counts.get(e['status'], 0) + 1
    assert counts == expected, f'{what}: executions by status {counts}, expected {expected}'


def test_failed_generations_do_not_strand_the_manifest(stack):
    """
    One manifest fails over and over while bursts of identical requests keep
    arriving on all instances; the instances are killed and restarted midway.
    Every burst after a failure must produce exactly one new attempt, until the
    index recovers and the manifest is produced and then served.
    """
    deployment, worker, make_fleet = stack
    fleet = make_fleet()
    manifest = Manifest.random()
    failures = random.randint(14, 16)
    restart_after = random.randint(5, 8)

    for round in range(1, failures + 1):
        what = f'round {round} (after {round - 1} failed attempt(s))'
        before = deployment.execution_arns()
        outcomes = burst(deployment, fleet, manifest, BURST)
        arn = assert_one_new_attempt(deployment, worker, before, outcomes, what)
        fail_attempt(deployment, worker, arn)
        assert_quiet(deployment, before | {arn}, f'{what}, after the attempt failed')
        assert_poll_reports_failure(deployment, fleet, outcomes[round % BURST], what)
        if round == restart_after:
            fleet.restart()

    what = f'round {failures + 1} (index recovered)'
    before = deployment.execution_arns()
    outcomes = burst(deployment, fleet, manifest, BURST)
    arn = assert_one_new_attempt(deployment, worker, before, outcomes, what)
    succeed_attempt(deployment, worker, arn)
    for i, outcome in enumerate(outcomes):
        assert_served(deployment, poll(deployment, fleet, outcome, i), f'{what}, client {i} polling')

    before = deployment.execution_arns()
    outcomes = burst(deployment, fleet, manifest, BURST)
    for i, outcome in enumerate(outcomes):
        assert_served(deployment, outcome, f'fresh request {i} after the manifest was produced')
    assert deployment.execution_arns() == before, 'a cached manifest was generated again'
    assert_history(deployment, {'FAILED': failures, 'SUCCEEDED': 1}, 'end state')


def test_manifest_survives_a_long_run_of_failures(stack):
    """
    A bad day: one manifest fails dozens of times in a row while the portal and
    the scripts keep asking for it, and the instances are restarted on the way.
    Once the index recovers, the next requests produce it. All of it has to fit
    in the account's Step Functions quotas.
    """
    deployment, worker, make_fleet = stack
    fleet = make_fleet()
    manifest = Manifest.random()
    failures = random.randint(42, 48)
    restart_after = random.randint(20, 26)

    for attempt in range(failures):
        what = f'burst {attempt + 1} (after {attempt} failed attempt(s))'
        before = deployment.execution_arns()
        outcomes = burst(deployment, fleet, manifest, BURST)
        arn = assert_one_new_attempt(deployment, worker, before, outcomes, what)
        fail_attempt(deployment, worker, arn)
        if attempt + 1 == restart_after:
            fleet.restart()

    what = f'after {failures} failed attempts, index recovered'
    before = deployment.execution_arns()
    outcomes = burst(deployment, fleet, manifest, BURST)
    arn = assert_one_new_attempt(deployment, worker, before, outcomes, what)
    succeed_attempt(deployment, worker, arn)
    for i, outcome in enumerate(outcomes):
        assert_served(deployment, poll(deployment, fleet, outcome, i), f'{what}, client {i} polling')
    assert_history(deployment, {'FAILED': failures, 'SUCCEEDED': 1}, 'end state')


def test_expired_manifest_whose_regeneration_failed(stack):
    """
    A manifest expires from the bucket; the clients that ask for it again share
    one regeneration, which fails. The next fresh requests must still get the
    manifest produced.
    """
    deployment, worker, make_fleet = stack
    fleet = make_fleet()
    manifest = Manifest.random()

    before = deployment.execution_arns()
    first = burst(deployment, fleet, manifest, 4)
    arn = assert_one_new_attempt(deployment, worker, before, first, 'first generation')
    succeed_attempt(deployment, worker, arn)
    served = poll(deployment, fleet, first[0])
    assert_served(deployment, served, 'first generation')
    object_key = served.body['Location'].split('/', 3)[3]
    aws_s3_delete(deployment, object_key)

    before = deployment.execution_arns()
    outcomes = burst(deployment, fleet, manifest, BURST)
    arn = assert_one_new_attempt(deployment, worker, before, outcomes, 'requests after the manifest expired')
    fail_attempt(deployment, worker, arn)
    assert_quiet(deployment, before | {arn}, 'after the regeneration failed')
    assert_poll_reports_failure(deployment, fleet, outcomes[3], 'regeneration')

    before = deployment.execution_arns()
    outcomes = burst(deployment, fleet, manifest, BURST)
    arn = assert_one_new_attempt(deployment, worker, before, outcomes, 'requests after the regeneration failed')
    succeed_attempt(deployment, worker, arn)
    for i, outcome in enumerate(outcomes):
        assert_served(deployment, poll(deployment, fleet, outcome, i), f'client {i} polling the regeneration')
    before = deployment.execution_arns()
    assert_served(deployment, poll(deployment, fleet, first[1]), 'a client still holding the first token')
    assert deployment.execution_arns() == before, 'polling an old token of a cached manifest started a generation'
    assert_history(deployment, {'FAILED': 1, 'SUCCEEDED': 2}, 'end state')


def test_aborted_generation(stack):
    """
    An operator aborts a hung generation with StopExecution. Its pollers are
    told it failed; the next requests get the manifest produced.
    """
    deployment, worker, make_fleet = stack
    fleet = make_fleet()
    manifest = Manifest.random()

    before = deployment.execution_arns()
    outcomes = burst(deployment, fleet, manifest, BURST)
    arn = assert_one_new_attempt(deployment, worker, before, outcomes, 'first attempt')
    aws('stepfunctions').stop_execution(executionArn=arn, error='Operator', cause='generation hung')
    assert deployment.wait_closed(arn) == 'ABORTED'
    assert_quiet(deployment, before | {arn}, 'after the attempt was aborted')
    assert_poll_reports_failure(deployment, fleet, outcomes[2], 'aborted attempt')

    before = deployment.execution_arns()
    outcomes = burst(deployment, fleet, manifest, BURST)
    arn = assert_one_new_attempt(deployment, worker, before, outcomes, 'requests after the abort')
    succeed_attempt(deployment, worker, arn)
    for i, outcome in enumerate(outcomes):
        assert_served(deployment, poll(deployment, fleet, outcome, i), f'client {i} polling')
    assert_history(deployment, {'ABORTED': 1, 'SUCCEEDED': 1}, 'end state')


def test_request_that_straggles_past_a_finished_generation(stack):
    """
    A request finds the manifest missing from the bucket, then stalls before it
    reaches Step Functions; meanwhile the running attempt succeeds and writes
    the manifest. The straggler must not generate the manifest again.
    """
    deployment, worker, make_fleet = stack
    fleet = make_fleet()
    proxy = Proxy(upstream=account.url)
    try:
        straggler = make_fleet(count=1, label='straggler', endpoint=proxy.url)
        manifest = Manifest.random()

        before = deployment.execution_arns()
        outcomes = burst(deployment, fleet, manifest, 4)
        arn = assert_one_new_attempt(deployment, worker, before, outcomes, 'first attempt')

        result: list = []
        proxy.arm()
        thread = threading.Thread(target=lambda: result.append(
            http_call('PUT', straggler.port(0), manifest.path(5))))
        thread.start()
        held = proxy.held.wait(timeout=10)
        print(f'straggler held before {proxy.held_target!r}' if held else 'straggler made no held call')
        succeed_attempt(deployment, worker, arn)
        proxy.release()
        thread.join(timeout=30)
        assert result, 'the straggling request never completed'
        outcome = settle(deployment, fleet, result[0], 0)
        assert_served(deployment, outcome, 'the straggling request')
        assert deployment.execution_arns() == {arn}, (
            f'the straggler generated the manifest again: {sorted(deployment.statuses().items())}')
        assert_history(deployment, {'SUCCEEDED': 1}, 'end state')
    finally:
        proxy.close()


def aws_s3_delete(deployment: Deployment, key: str):
    aws('s3').delete_object(Bucket=deployment.bucket, Key=key)
    assert not deployment.object_exists(key)
