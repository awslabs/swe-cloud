"""Verifier: runs the relay as separate processes against Floci SQS and grades what SQS did.

Each case uses a fresh queue, its own proxy per relay, its own downstream and a probe that
receives with VisibilityTimeout=0 throughout. Cases are independent; none needs another to pass.
"""

import os

import pytest
import boto3
from pathlib import Path
from cdk_seed.cfn_deploy import deploy

import rig
from checks import Evidence
from rig import Case, ReceiveGapRule, SlowExtensionRule, SlowReceiveAnswerRule, now, sleep_until, wait_for

LOG_ROOT = os.environ.get("VERIFIER_LOG_ROOT", "/logs/verifier/cases")

BATCH = 10
FIRST_RECEIVE_OFFSET = 5.0   # the jobs arrive this long after the relay's first receive request
RECEIVE_GAP = 17.0           # 0.85 V: the other receive of that poll comes back this late
SLOW_EXTENSION = 17.0        # 0.85 V: the response to the first extension comes back this late
SLOW_RECEIVE_ANSWER = 14.0   # 0.7 V: the answer of the receive carrying the jobs comes back this late
KILL_AT = 40.0
STOP_IN_GAP_AT = 8.0
STOP_BEHIND_SLOW_AT = 28.0


@pytest.fixture(scope="module", autouse=True)
def floci_is_live():
    try:
        client = rig.floci_client()
        url = deploy(
            boto3.client("cloudformation", endpoint_url=rig.FLOCI_URL, region_name=rig.REGION,
                         aws_access_key_id="test", aws_secret_access_key="test"),
            "seed-verifier-preflight",
            Path(__file__).parent / "cdk_seed/probe.template.json",
            QueueName="verifier-preflight")["QueueUrl"]
        client.get_queue_attributes(QueueUrl=url, AttributeNames=["All"])
    except Exception as error:  # pragma: no cover - infrastructure
        pytest.exit(f"environment: Floci SQS is not answering: {error!r}", returncode=3)


def run(case, scenario):
    failures = []
    try:
        failures = scenario(case)
    except AssertionError as error:
        failures = [str(error)]
    finally:
        try:
            case.close()
        finally:
            case.dump({"failures": failures})
    return failures


def start_behind_receive_gap(case, hold_rule=None, extra_env=None):
    """Starts relay-1 on an empty queue; FIRST_RECEIVE_OFFSET s after its first receive request the
    jobs arrive and are returned by one receive of a poll, while the poll's other receive comes back
    RECEIVE_GAP s later (or as ``hold_rule`` says)."""
    proxy = case.proxy("relay-1")
    proxy.hold_rule = hold_rule or ReceiveGapRule(RECEIVE_GAP)
    relay = case.start_relay("relay-1", extra_env)
    assert wait_for(lambda: any(r["action"] == "ReceiveMessage" for r in proxy.snapshot()), 60), \
        "relay-1 never sent a ReceiveMessage"
    first = min(r["t_req"] for r in proxy.snapshot() if r["action"] == "ReceiveMessage")
    case.notes["first_receive_request"] = first
    sleep_until(first + 1.0)
    proxy.gate()
    assert wait_for(lambda: proxy.outstanding_receives() == 0 and proxy.gated_count() >= 1, 30), \
        "relay-1 stopped polling"
    sleep_until(max(now() + 0.3, first + FIRST_RECEIVE_OFFSET))
    jobs = case.send_jobs(BATCH)
    proxy.release()
    assert wait_for(lambda: proxy.first_messages_forwarded_at is not None, 30), \
        "relay-1 did not receive the jobs"
    r = proxy.first_messages_forwarded_at
    case.notes["jobs_received"] = r
    return relay, jobs, r


def test_quick_batch_costs_no_extension():
    case = Case("quick", lambda relay, ordinal, job: 0.15, LOG_ROOT)

    def scenario(case):
        jobs = case.send_jobs(BATCH)
        relay = case.start_relay("relay-1")
        wait_for(case.drained, 60, interval=0.5)
        relay.finish()
        evidence = Evidence(case)
        failures = evidence.standard_checks() + evidence.check_everything_routed_and_queue_empty()
        for job in jobs:
            mid = case.jobs[job]
            deliveries = evidence.deliveries_of("relay-1", mid)
            routes = [r for r in evidence.routes_of("relay-1", mid) if r["status"] == 200]
            if not deliveries or not routes:
                continue
            quick = evidence.route_end(routes[0]) - deliveries[0]["r"] <= rig.VISIBILITY_TIMEOUT / 4
            if quick and evidence.extensions_of("relay-1", mid):
                failures.append(f"{job} was taken by the downstream within "
                                f"{evidence.route_end(routes[0]) - deliveries[0]['r']:.1f} s of its receive and was "
                                f"still extended")
        return failures

    failures = run(case, scenario)
    assert not failures, "\n".join(failures)


def test_crash_after_slow_receive():
    plan = lambda relay, ordinal, job: 30.0 if (relay, ordinal) == ("relay-1", 1) else 1.0
    case = Case("crash", plan, LOG_ROOT)

    def scenario(case):
        relay, jobs, r = start_behind_receive_gap(case)
        sleep_until(r + KILL_AT)
        t_kill = relay.sigkill()
        relay.wait_exit(10)
        wait_for(lambda: all(case.probe.seen_after(case.jobs[j], t_kill) for j in jobs),
                 max(0.0, t_kill + rig.VISIBILITY_TIMEOUT + 10 - now()), interval=0.5)
        case.drain_with("relay-2")
        evidence = Evidence(case)
        failures = evidence.check_poll_kept_shape("relay-1")
        failures += evidence.standard_checks()
        failures += evidence.check_recovery_after_kill("relay-1", [case.jobs[j] for j in jobs])
        failures += evidence.check_everything_routed_and_queue_empty()
        return failures

    failures = run(case, scenario)
    assert not failures, "\n".join(failures)


def _stop_scenario(stop_at, exit_wait):
    def scenario(case):
        relay, jobs, r = start_behind_receive_gap(case)
        sleep_until(r + stop_at)
        relay.sigterm()
        relay.wait_exit(max(0.0, r + exit_wait - now()))
        wait_for(lambda: all(case.probe.seen_after(case.jobs[j], r) or case.routed_ok(j) for j in jobs),
                 max(0.0, relay.t_sigterm + rig.VISIBILITY_TIMEOUT + 10 - now()), interval=0.5)
        relay.finish(grace=1)
        case.drain_with("relay-2")
        evidence = Evidence(case)
        failures = evidence.check_poll_kept_shape("relay-1")
        failures += evidence.standard_checks()
        stop_failures, _ = evidence.check_stop("relay-1", [case.jobs[j] for j in jobs])
        failures += stop_failures
        failures += evidence.check_everything_routed_and_queue_empty()
        return failures
    return scenario


def test_stop_while_poll_outstanding():
    plan = lambda relay, ordinal, job: 30.0 if (relay, ordinal) == ("relay-1", 1) else 1.0
    case = Case("stop-early", plan, LOG_ROOT)
    failures = run(case, _stop_scenario(STOP_IN_GAP_AT, exit_wait=RECEIVE_GAP + 20))
    assert not failures, "\n".join(failures)


def test_stop_while_handing_a_slow_job():
    plan = lambda relay, ordinal, job: 40.0 if (relay, ordinal) == ("relay-1", 1) else 1.0
    case = Case("stop-late", plan, LOG_ROOT)
    failures = run(case, _stop_scenario(STOP_BEHIND_SLOW_AT, exit_wait=RECEIVE_GAP + 40 + 20))
    assert not failures, "\n".join(failures)


def test_slow_receive_answer():
    """SQS hands the jobs out at once, but its answer reaches the relay SLOW_RECEIVE_ANSWER s later;
    the poll's other receive waits out the production long poll."""
    plan = lambda relay, ordinal, job: 30.0 if (relay, ordinal) == ("relay-1", 1) else 1.0
    case = Case("slow-receive", plan, LOG_ROOT)

    def scenario(case):
        relay, jobs, r = start_behind_receive_gap(
            case, SlowReceiveAnswerRule(SLOW_RECEIVE_ANSWER), {"WAIT_TIME_SECONDS": "10"})
        wait_for(case.drained, 150, interval=0.5)
        relay.finish()
        if not case.drained():
            case.drain_with("relay-2")
        evidence = Evidence(case)
        return evidence.standard_checks() + evidence.check_everything_routed_and_queue_empty()

    failures = run(case, scenario)
    assert not failures, "\n".join(failures)


def test_slow_extension_response():
    plan = lambda relay, ordinal, job: 60.0 if (relay, ordinal) == ("relay-1", 1) else 1.0
    case = Case("slow-extension", plan, LOG_ROOT)

    def scenario(case):
        jobs = case.send_jobs(1)
        mid = case.jobs[jobs[0]]
        proxy = case.proxy("relay-1")
        rule = SlowExtensionRule(
            lambda: {m["handle"] for rec in proxy.snapshot() for m in rec["messages"] if m["message_id"] == mid},
            SLOW_EXTENSION)
        proxy.hold_rule = rule
        relay = case.start_relay("relay-1")
        wait_for(case.drained, 150, interval=0.5)
        relay.finish()
        if not case.drained():
            case.drain_with("relay-2")
        evidence = Evidence(case)
        failures = evidence.standard_checks() + evidence.check_everything_routed_and_queue_empty()
        if not rule.fired:
            failures.append(f"relay-1 never extended {jobs[0]} while the downstream held it for 60 s")
        return failures

    failures = run(case, scenario)
    assert not failures, "\n".join(failures)
