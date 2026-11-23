"""Verifier: one live run of the reliability pipeline under worker deaths, a slow Notify,
concurrent and redelivered copies, repeated delivery-status callbacks, the state the previous
version left behind when it was replaced, and the finished submissions the table has held since
launch (most older than Notify's retention), all behind Notify's per-key rate limit.

Each check is its own test so a failure names what went wrong (a duplicate email, a lost
response, a record that does not match what Notify sent, a queue left non-empty).
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa: E402

DONE = ("SENT", "DELIVERED")


@pytest.fixture(scope="session")
def run():
    result = harness.run_scenario()
    if result.get("error", "") and str(result["error"]).startswith("environment"):
        pytest.fail(result["error"])
    return result


def _describe(run, name):
    sends = run["sends"][name]
    item = (run.get("final") or {}).get("items", {}).get(name)
    return json.dumps({
        "submission": run["subs"][name]["sid"],
        "sends": [{"id": n["id"], "reference": n["reference"], "t": round(n["t"] - run.get("t0", 0), 2)}
                  for n in sends],
        "item": {k: v for k, v in (item or {}).items() if k not in ("FormData",)},
    }, indent=1)


def _check(run, name, expected_sends, states=DONE):
    assert run.get("final") is not None, f"no final state was read: {run.get('error')}"
    sends = run["sends"][name]
    item = run["final"]["items"][name]
    detail = _describe(run, name)
    if len(sends) > expected_sends:
        pytest.fail(f"duplicate email: {len(sends)} accepted sends, expected {expected_sends}\n{detail}")
    if len(sends) < expected_sends:
        pytest.fail(f"response not (re)sent: {len(sends)} accepted sends, expected {expected_sends}\n{detail}")
    assert item, f"ReliabilityQueue item is missing\n{detail}"
    assert item.get("NotifyState") in states, f"NotifyState {item.get('NotifyState')!r} is not one of {states}\n{detail}"
    assert item.get("NotificationId") == sends[-1]["id"], (
        f"NotificationId {item.get('NotificationId')!r} is not the email Notify accepted last "
        f"({sends[-1]['id']})\n{detail}")


def test_harness_ran(run):
    assert run.get("error") is None, run.get("error")
    assert not run["stub"]["violations"], run["stub"]["violations"]


def test_plain_submission_is_sent_once(run):
    _check(run, "B1", 1)


def test_concurrent_copies_send_once(run):
    _check(run, "S4", 1)


def test_worker_killed_after_notify_accepted(run):
    _check(run, "S1", 1)


def test_worker_killed_before_notify_accepted(run):
    _check(run, "S2", 1)


def test_slow_notify_response(run):
    _check(run, "S5", 1)


def test_temporary_failure_gets_one_replacement(run):
    _check(run, "S3", 2)


def test_permanent_failure_is_final(run):
    _check(run, "S6", 1, states=("SENT", "DELIVERED", "FAILED"))


def test_bounce_before_the_send_is_recorded(run):
    _check(run, "S7", 2)


# Submissions the previous version was in the middle of when it was replaced by the deploy.
def test_deployed_while_send_unrecorded(run):
    _check(run, "L1", 1)


def test_deployed_after_bounce_reset(run):
    _check(run, "L2", 2)


def test_deployed_after_stale_bounce_reset(run):
    _check(run, "L3", 2)


def test_deployed_while_replacement_unrecorded(run):
    _check(run, "L4", 2)


def test_deployed_then_bounced(run):
    _check(run, "L5", 2)


def test_deployed_dead_lettered(run):
    _check(run, "L6", 1)


def test_deployed_while_replacement_in_flight(run):
    _check(run, "L7", 2)


def test_deployed_after_single_bounce_reset(run):
    _check(run, "L8", 2)


@pytest.mark.parametrize("role", ["submission", "reprocess", "dlq"])
def test_queue_drained(run, role):
    assert run.get("final") is not None, run.get("error")
    counts = run["final"]["queues"][role]
    left = {k: counts.get(k) for k in ("ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible",
                                       "ApproximateNumberOfMessagesDelayed")}
    assert all(int(v or 0) == 0 for v in left.values()), f"{role} queue not empty at the end: {left}"


def test_queue_configuration_untouched(run):
    assert run.get("final") is not None, run.get("error")
    for role in ("submission", "reprocess"):
        attributes = run["final"]["queues"][role]
        redrive = json.loads(attributes.get("RedrivePolicy") or "{}")
        assert int(redrive.get("maxReceiveCount", 0)) == harness.MAX_RECEIVE_COUNT, (role, redrive)
        assert int(attributes.get("VisibilityTimeout", 0)) == harness.VISIBILITY_TIMEOUT_S, (role, attributes)
    if run.get("queues_after") is not None:
        expected = set(run["queues_before"]) | {url.rstrip("/").rsplit("/", 1)[-1] for url in run["queues"].values()}
        extra = sorted(set(run["queues_after"]) - expected)
        assert not extra, f"the application created queues: {extra}"


# Submissions finished long before the deploy. The table keeps every one (items are never
# deleted); Notify keeps their emails for 7 days only.
def test_finished_responses_not_sent_again(run):
    history = run.get("history")
    assert history and history["count"] == harness.HISTORY_OLD + harness.HISTORY_RECENT, history
    assert not history["resent_count"], (
        f"{history['resent_count']} responses that were sent before the deploy were emailed again "
        f"(first ones: {json.dumps(history['resent'], indent=1)})")


def test_finished_records_kept(run):
    history = run.get("history")
    assert history and history["count"] == harness.HISTORY_OLD + harness.HISTORY_RECENT, history
    assert not history["changed_count"], (
        f"{history['changed_count']} finished submissions no longer say SENT/DELIVERED with the email "
        f"the owner received (first ones: {json.dumps(history['changed'], indent=1, default=str)})")
