"""Verifier: drives the dispatch CLI of the release in production and the fixed workers on the
emulator, and grades the observable outcomes (which dispatches are accepted or dropped, which
refreshes run, freshness).

Each test uses its own queue, tables, entity and workers, so a failure in one never hides another.
"""

import os
import sys
import time
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rig as harness  # noqa: E402


@pytest.fixture
def rig(request):
    r = harness.Rig(request.node.name)
    yield r
    r.stop()
    print(r.report())


def entity(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:6]}"


def test_dedup_and_refresh(rig):
    """Unfaulted path: a second edit while the first refresh waits is folded into it."""
    rig.start_workers(2)
    e = entity("sku")
    first = rig.dispatch(e, delay=4, tag="first")
    assert first["status"] == "ACCEPTED"
    second = rig.dispatch(e, tag="second (first still waiting)")
    assert second["status"] == "DROPPED", "a dispatch while a refresh waits must be dropped"
    row = rig.wait_start(e, 1)
    assert row["message_id"] == first["message_id"]
    rig.close_out(e, 2, {first["message_id"]})


def test_put_back_job_releases_when_it_runs(rig):
    """A job put back by the overlap guard is still the waiting copy; when it finally runs (a
    later delivery), it gives up the lock like any other start."""
    rig.start_workers(2)
    e = entity("bundle")
    rig.barrier(e, 1)
    running = rig.dispatch(e, tag="J0")
    assert running["status"] == "ACCEPTED"
    rig.wait_start(e, 1)

    waiting = rig.dispatch(e, tag="J1 (J0 running)")
    assert waiting["status"] == "ACCEPTED", "a dispatch after the refresh started must be accepted"
    t1 = time.time()
    rig.sleep_until(t1 + 4)
    dup = rig.dispatch(e, tag="J1b (J1 waiting behind J0)")
    assert dup["status"] == "DROPPED", (
        "J1 had been put back by the overlap guard and was still waiting, yet a new dispatch "
        "was accepted")
    rig.barrier(e, 2)
    rig.sleep_until(t1 + 8)
    rig.go(e, 1)

    row = rig.wait_start(e, 2, what="J1 after J0 finished")
    assert row["message_id"] == waiting["message_id"]
    later = rig.dispatch(e, tag="J2 (J1 running)")
    assert later["status"] == "ACCEPTED", (
        "J1 was running (it had been put back first), but a dispatch made during its refresh "
        "was dropped: its dedup lock was never given up")
    time.sleep(2)
    rig.go(e, 2)
    row = rig.wait_start(e, 3, what="J2")
    assert row["message_id"] == later["message_id"]
    rig.close_out(e, 4, {running["message_id"], waiting["message_id"], later["message_id"]})


def test_retry_does_not_free_newer_dispatch(rig):
    """A refresh that fails after it started is retried; the dispatch accepted meanwhile is the
    waiting copy and keeps the entity deduplicated."""
    rig.start_workers(2)
    e = entity("price")
    rig.barrier(e, 1, fail=True)
    rig.barrier(e, 2)
    k1 = rig.dispatch(e, tag="K1")
    assert k1["status"] == "ACCEPTED"
    t1 = time.time()
    rig.wait_start(e, 1)
    rig.sleep_until(t1 + 4)
    k2 = rig.dispatch(e, delay=25, tag="K2 (K1 running, delay 25)")
    assert k2["status"] == "ACCEPTED", "a dispatch after the refresh started must be accepted"
    t2 = time.time()
    rig.go(e, 1)

    row = rig.wait_start(e, 2, what="K1's retry")
    assert row["message_id"] == k1["message_id"], "K1's retry must be the same SQS message"
    k3 = rig.dispatch(e, tag="K3 (K2 waiting, K1 retry running)")
    assert k3["status"] == "DROPPED", (
        "K2 was waiting in the queue with a live lock, but a new dispatch was accepted while "
        "K1's retry ran: two copies waiting")
    rig.go(e, 2)
    rig.sleep_until(t2 + 15)
    k3b = rig.dispatch(e, tag="K3b (K2 still waiting)")
    assert k3b["status"] == "DROPPED", "K2 was still waiting with a live lock"

    row = rig.wait_start(e, 3, timeout=40, what="K2")
    assert row["message_id"] == k2["message_id"]
    rig.close_out(e, 4, {k1["message_id"], k2["message_id"]})


def test_lapsed_lock_taken_over(rig):
    """A copy stuck behind a long refresh outlives its --unique-for; the next dispatch takes the
    lock over and becomes the waiting copy. The stuck copy running later must not undo that."""
    rig.start_workers(2)
    e = entity("import")
    rig.barrier(e, 1)
    running = rig.dispatch(e, tag="J0")
    assert running["status"] == "ACCEPTED"
    rig.wait_start(e, 1)

    stuck = rig.dispatch(e, unique_for=15, tag="J1 (unique-for 15, J0 running)")
    assert stuck["status"] == "ACCEPTED"
    t1 = time.time()
    rig.sleep_until(t1 + 5)
    dup = rig.dispatch(e, tag="J1b (J1 waiting, lock live)")
    assert dup["status"] == "DROPPED"
    rig.sleep_until(t1 + 18)
    takeover = rig.dispatch(e, delay=30, tag="J3 (J1's lock lapsed, delay 30)")
    assert takeover["status"] == "ACCEPTED", "J1's lock had lapsed; the next dispatch is accepted"
    rig.barrier(e, 2)
    rig.sleep_until(t1 + 20)
    rig.go(e, 1)

    row = rig.wait_start(e, 2, what="J1 after J0 finished")
    assert row["message_id"] == stuck["message_id"]
    j4 = rig.dispatch(e, tag="J4 (J3 waiting, J1 running)")
    assert j4["status"] == "DROPPED", (
        "J3 had taken over the lapsed lock and was waiting, but a dispatch made while the stuck "
        "J1 ran was accepted: J3's lock was removed")
    rig.go(e, 2)
    row = rig.wait_start(e, 3, timeout=45, what="J3")
    assert row["message_id"] == takeover["message_id"]
    rig.close_out(e, 4, {running["message_id"], stuck["message_id"], takeover["message_id"]})


def test_lock_lapsed_before_first_delivery(rig):
    """A job that waited in the queue past its --unique-for before any worker saw it no longer
    holds the lock; the dispatch that took the lapsed lock over is the waiting copy, also once the
    first job is put back and then runs."""
    rig.start_workers(2)
    e = entity("backlog")
    rig.barrier(e, 1)
    running = rig.dispatch(e, tag="J0")
    assert running["status"] == "ACCEPTED"
    rig.wait_start(e, 1)

    late = rig.dispatch(e, delay=20, unique_for=10, tag="M (J0 running; delay 20, unique-for 10)")
    assert late["status"] == "ACCEPTED", "a dispatch after the refresh started must be accepted"
    t1 = time.time()
    rig.sleep_until(t1 + 4)
    dup = rig.dispatch(e, tag="M' (M waiting, lock live)")
    assert dup["status"] == "DROPPED"
    rig.sleep_until(t1 + 12)
    takeover = rig.dispatch(e, delay=35, tag="L (M's lock lapsed, M not yet delivered; delay 35)")
    assert takeover["status"] == "ACCEPTED", "M's lock had lapsed; the next dispatch is accepted"
    rig.barrier(e, 2)
    rig.sleep_until(t1 + 27)     # M becomes visible at +20 and is put back behind J0
    rig.go(e, 1)

    row = rig.wait_start(e, 2, what="M after J0 finished")
    assert row["message_id"] == late["message_id"]
    l2 = rig.dispatch(e, tag="L2 (L waiting, M running)")
    assert l2["status"] == "DROPPED", (
        "L had taken over M's lapsed lock and was waiting, but a dispatch made while M ran was "
        "accepted: M's start removed L's lock")
    rig.go(e, 2)
    row = rig.wait_start(e, 3, timeout=45, what="L")
    assert row["message_id"] == takeover["message_id"]
    rig.close_out(e, 4, {running["message_id"], late["message_id"], takeover["message_id"]})


def test_retry_of_job_started_before_the_cutover(rig):
    """The old worker fleet started a job that failed; its retry runs on the fixed workers and
    must leave the dispatch accepted meanwhile holding the lock."""
    rig.start_workers(1, current_release=True, BACKOFF=12)
    e = entity("cutover")
    rig.barrier(e, 1, fail=True)
    rig.barrier(e, 2)
    k1 = rig.dispatch(e, tag="K1")
    assert k1["status"] == "ACCEPTED"
    t1 = time.time()
    rig.wait_start(e, 1)
    rig.sleep_until(t1 + 4)
    k2 = rig.dispatch(e, delay=30, tag="K2 (K1 running on the old fleet, delay 30)")
    assert k2["status"] == "ACCEPTED"
    t2 = time.time()
    rig.go(e, 1)
    rig.wait_end(e, 1)
    rig.stop_workers()
    rig.start_workers(2)

    row = rig.wait_start(e, 2, timeout=30, what="K1's retry on the fixed workers")
    assert row["message_id"] == k1["message_id"]
    k3 = rig.dispatch(e, tag="K3 (K2 waiting, K1 retry running)")
    assert k3["status"] == "DROPPED", (
        "K2 was waiting with a live lock, but a dispatch was accepted while the retry of a job "
        "the old fleet had started ran: the duplicate pileup of 2.4.0 is back")
    rig.go(e, 2)
    rig.sleep_until(t2 + 15)
    k3b = rig.dispatch(e, tag="K3b (K2 still waiting)")
    assert k3b["status"] == "DROPPED"
    row = rig.wait_start(e, 3, timeout=40, what="K2")
    assert row["message_id"] == k2["message_id"]
    rig.close_out(e, 4, {k1["message_id"], k2["message_id"]})


def _first_receive_second(rig, message_id, timeout=3):
    """Seconds field (0-59) of the worker log line for the message's first delivery, or None."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for _, _, cwd in rig.active:
            for line in rig._log(cwd).splitlines():
                if f"received message_id={message_id} attempt=1" in line:
                    return int(line.split()[0].split(":")[2])
        time.sleep(0.05)
    return None


def test_job_put_back_before_the_cutover(rig):
    """The old worker fleet put a job back behind a running refresh; that job is still the waiting
    copy at the switch. When it starts on the fixed workers (a later delivery) it gives up the
    lock like any other start."""
    rig.start_workers(3, current_release=True, RELEASE_AFTER=15)
    # The usual case for an immediate dispatch is that an idle worker receives the job within the
    # second its lock was taken. Up to three tries (each on a fresh entity) to get that case.
    for attempt in range(3):
        e = entity("handover")
        rig.barrier(e, 1)
        t = time.time()
        running = rig.dispatch(e, tag="J0")
        took = time.time() - t
        assert running["status"] == "ACCEPTED"
        t0 = time.time()
        rig.wait_start(e, 1)
        rig.sleep_until(t0 + 4)
        now = time.time()
        rig.sleep_until(now + ((0.35 - took - now) % 1.0))
        waiting = rig.dispatch(e, tag="J1 (J0 running on the old fleet)")
        assert waiting["status"] == "ACCEPTED", "a dispatch after the refresh started must be accepted"
        received = _first_receive_second(rig, waiting["message_id"])
        lock = rig.ddb.get_item(TableName=rig.unique_table, ConsistentRead=True,
                                Key={"pk": {"S": f"refresh_unique:RefreshEntity:{e}"}}).get("Item")
        taken = (int(lock["expires_at"]["N"]) - harness.UNIQUE_FOR) % 60 if lock else None
        rig.note(f"J1 lock taken at :{taken}, first received at :{received}")
        if (received is not None and received == taken) or attempt == 2:
            break
        rig.go(e, 1)             # not that case: let this entity run out and try another
        rig.wait_end(e, 1)

    rig.barrier(e, 2)
    time.sleep(2)                # the idle old worker has put J1 back for 15 s
    rig.go(e, 1)
    rig.wait_end(e, 1)
    rig.stop_workers()
    rig.start_workers(2)

    row = rig.wait_start(e, 2, what="J1 on the fixed workers after the old fleet put it back")
    assert row["message_id"] == waiting["message_id"]
    later = rig.dispatch(e, tag="J2 (J1 running)")
    assert later["status"] == "ACCEPTED", (
        "J1 was running on the fixed workers (the old fleet had put it back first), but a dispatch "
        "made during its refresh was dropped: its dedup lock was never given up")
    time.sleep(2)
    rig.go(e, 2)
    row = rig.wait_start(e, 3, what="J2")
    assert row["message_id"] == later["message_id"]
    rig.close_out(e, 4, {running["message_id"], waiting["message_id"], later["message_id"]})
