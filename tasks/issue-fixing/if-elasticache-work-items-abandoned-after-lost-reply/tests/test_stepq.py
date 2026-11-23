"""Verifier for stepq. One scenario runs once (module fixture); each test grades one outcome."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import harness  # noqa: E402

DISPATCH_SLO_MS = 3000
RECOVERY_SLO_MS = 15000
CLOCK_TOLERANCE_MS = 250


@pytest.fixture(scope="module")
def sc():
    return harness.run_scenario()


def _item(sc, label):
    t = sc.tracked_by_label(label)
    assert t is not None, f"scenario never reached item {label}: {sc.errors[-1:] or 'see scenario.json'}"
    return t


def _executor_runs(sc, t):
    """Runs the step runner started for t: one per new (item_id, lease_id) pair; attaching POSTs aren't runs."""
    return [r for r in sc.runs if r.item_id == t.item_id and r.executor != "batch"]


def _runs_text(sc, runs):
    return ", ".join(f"{r.executor}@+{r.start_ms - sc.t0}ms lease={r.lease_id}" for r in runs)


def _lease_lapses(sc, t, start, end, executor=None):
    """Samples taken while a run of t was open at which t's stored lease was missing, expired, or a
    lease another party sent (not the running executor's)."""
    bad = []
    seen = 0
    for at, vt, state in sc.samples:
        if not (start + 50 <= at <= end - 50):
            continue
        seen += 1
        lease = state.get(t.item_id)
        if lease in (None, "absent", "corrupt") or not isinstance(lease, str):
            bad.append((at - start, lease))
            continue
        expiry = harness.ulid_time(lease)
        if expiry is None or expiry < vt - CLOCK_TOLERANCE_MS:
            bad.append((at - start, lease))
            continue
        owner = t.lease_owner.get(lease)
        if executor is not None and owner is not None and owner != executor:
            bad.append((at - start, f"{lease} (sent by {owner})"))
    assert seen or end - start < 300, (
        f"verifier sampler recorded nothing while {t.label} ran ({end - start} ms): {sc.errors[-1:]}")
    return bad


def test_executors_start_and_load_the_library(sc):
    assert all(sc.ready.values()) and sc.ready, f"executors did not all print READY: {sc.ready}; {sc.errors[-1:]}"
    for name in ("q_lease", "q_extend", "q_requeue", "q_dequeue", "q_scavenge"):
        assert name in sc.functions, f"function {name} not loaded (loaded: {sc.functions})"


def test_due_items_start_within_the_dispatch_slo(sc):
    plain = [t for t in sc.items.values() if t.kind == "plain"]
    assert plain, "no plain items were enqueued"
    for t in plain:
        runs = _executor_runs(sc, t)
        assert len(runs) == 1, f"{t.label}: expected one run, saw {len(runs)}"
        assert runs[0].start_ms - t.enqueued_ms <= DISPATCH_SLO_MS, (
            f"{t.label}: started {runs[0].start_ms - t.enqueued_ms} ms after enqueue")
        assert t.removed_ms is not None, f"{t.label} was never removed from the queue"


def test_item_whose_lease_reply_was_lost_starts_promptly(sc):
    t = _item(sc, "X")
    assert t.fired, "no call naming X ever took its lease (q_lease contract)"
    runs = _executor_runs(sc, t)
    assert runs, f"X never ran; its lease committed at +{t.fired_ms - sc.t0} ms and the reply was lost"
    assert len(runs) == 1, f"X ran {len(runs)} times"
    delay = runs[0].start_ms - t.fired_ms
    assert delay <= DISPATCH_SLO_MS, f"X started {delay} ms after its lease committed (SLO {DISPATCH_SLO_MS} ms)"
    assert t.removed_ms is not None, "X was never removed from the queue"


def test_long_step_with_slow_calls_runs_once_and_keeps_its_lease(sc):
    t = _item(sc, "Y")
    runs = _executor_runs(sc, t)
    assert runs, "Y never ran"
    timeouts = [p for p in sc.posts if p["item_id"] == t.item_id and p["answer"] == 504]
    assert len(runs) == 1, (
        f"Y ran {len(runs)} times: {_runs_text(sc, runs)} (the load balancer answered 504 to {len(timeouts)} "
        f"of its POSTs while its step went on)")
    run = runs[0]
    lapses = _lease_lapses(sc, t, run.start_ms, run.end_ms or harness.now_ms(), run.executor)
    assert not lapses, (
        f"Y's lease was missing, expired or another holder's while its step ran, at (ms into run, lease): {lapses[:5]}")
    assert t.removed_ms is not None, "Y was never removed from the queue"


def test_item_held_by_the_batch_fleet_is_left_alone(sc):
    t = _item(sc, "W")
    assert t.fired, "no executor call ever named W"
    lease = [c for c in sc.batch_calls if c["call"] == "q_lease"]
    assert lease and lease[0]["rc"] == 0, f"batch fleet's q_lease of the unleased W failed: {lease}"
    runs = _executor_runs(sc, t)
    assert not runs, "W ran on an executor while the batch fleet held it: " + ", ".join(
        f"{r.executor}@+{r.start_ms - sc.t0}ms" for r in runs)
    extends = [c for c in sc.batch_calls if c["call"] == "q_extend"]
    assert extends and all(c["rc"] == 0 for c in extends), f"batch fleet's q_extend calls on W failed: {extends}"
    dequeue = [c for c in sc.batch_calls if c["call"] == "q_dequeue"]
    assert dequeue and dequeue[0]["rc"] == 0, f"batch fleet's q_dequeue of W failed: {dequeue}"
    assert t.removed_ms is not None, "W was still in the queue after the batch fleet completed it"


def test_item_leased_as_its_executor_is_restarted_starts_promptly(sc):
    t = _item(sc, "S")
    assert t.fired and sc.restarted, "no call naming S ever took its lease (q_lease contract)"
    runs = _executor_runs(sc, t)
    assert runs, (f"S never ran; its lease committed at +{t.fired_ms - sc.t0} ms, the reply was lost and "
                  f"{sc.restarted['executor']} was sent SIGTERM")
    assert len(runs) == 1, f"S ran {len(runs)} times: {_runs_text(sc, runs)}"
    delay = runs[0].start_ms - t.fired_ms
    assert delay <= DISPATCH_SLO_MS, (
        f"S started {delay} ms after its lease committed (SLO {DISPATCH_SLO_MS} ms); its executor "
        f"{sc.restarted['executor']} was sent SIGTERM at that moment")
    assert t.removed_ms is not None, "S was never removed from the queue"


def test_failed_step_is_run_again_promptly(sc):
    t = _item(sc, "F")
    runs = _executor_runs(sc, t)
    assert runs, "F never ran"
    assert len(runs) >= 2, f"F's step failed (500) once and never ran again: {_runs_text(sc, runs)}"
    assert len(runs) == 2, f"F ran {len(runs)} times; only its first run failed: {_runs_text(sc, runs)}"
    gap = runs[1].start_ms - (runs[0].end_ms or runs[1].start_ms)
    assert gap <= DISPATCH_SLO_MS, f"F started again {gap} ms after its failed run (SLO {DISPATCH_SLO_MS} ms)"
    assert t.removed_ms is not None, "F was never removed from the queue"


def test_item_whose_executor_died_after_its_step_is_not_run_again(sc):
    t = _item(sc, "D")
    runs = _executor_runs(sc, t)
    assert runs and t.fired and sc.died_after_step, "D's step never finished on an executor"
    dead = sc.died_after_step["executor"]
    assert len(runs) == 1, (
        f"D ran {len(runs)} times: {_runs_text(sc, runs)}; its step had finished when {dead}'s host died")
    back = [p for p in sc.posts if p["item_id"] == t.item_id and p["t"] > t.fired_ms]
    assert back, f"no executor sent D to the step runner again after {dead}'s host died"
    first = back[0]
    assert first["executor"] != dead, f"D was sent again by the dead executor {dead}"
    lease = t.committed_lease
    assert lease, "D had no stored lease when its holder died"
    expiry = harness.ulid_time(lease)
    assert first["t"] >= expiry - CLOCK_TOLERANCE_MS, (
        f"D was sent again {expiry - first['t']} ms before the dead holder's lease expired")
    taken = t.lease_seen.get(lease, t.fired_ms)
    assert first["t"] - taken <= RECOVERY_SLO_MS, (
        f"D was sent again {first['t'] - taken} ms after the dead holder took its lease (SLO {RECOVERY_SLO_MS} ms)")
    assert t.removed_ms is not None, "D was never removed from the queue"


def test_item_of_a_dead_executor_runs_once_on_a_survivor(sc):
    t = _item(sc, "Z")
    assert t.fired and sc.killed, "no executor took Z's lease, so no holder was killed"
    expiry = harness.ulid_time(t.committed_lease)
    runs = _executor_runs(sc, t)
    assert runs, f"Z was never run again after its holder {sc.killed} died"
    first = runs[0]
    assert first.executor != sc.killed, f"Z ran on the killed executor {sc.killed}"
    assert first.start_ms >= expiry - CLOCK_TOLERANCE_MS, (
        f"Z started {expiry - first.start_ms} ms before the dead holder's lease expired")
    assert first.start_ms - t.fired_ms <= RECOVERY_SLO_MS, (
        f"Z started {first.start_ms - t.fired_ms} ms after the dead holder's lease (SLO {RECOVERY_SLO_MS} ms)")
    assert sc.foreign_scavenge and "rc" in sc.foreign_scavenge, "the batch fleet's scavenger pass never ran"
    assert len(runs) == 1, f"Z ran {len(runs)} times: {_runs_text(sc, runs)}"
    lapses = _lease_lapses(sc, t, first.start_ms, first.end_ms or harness.now_ms(), first.executor)
    assert not lapses, f"Z's lease was missing, expired or another holder's while its step ran: {lapses[:5]}"
    assert t.removed_ms is not None, "Z was never removed from the queue"


def test_every_run_holds_the_stored_lease_and_never_overlaps(sc):
    assert sc.runs, "nothing ran"
    for r in sc.runs:
        assert r.lease_id, f"{r.label} was sent to the step runner by {r.executor} with no lease_id"
        assert isinstance(r.stored_lease, str) and r.stored_lease, (
            f"{r.label} started on {r.executor} (lease_id {r.lease_id}) while the item held no lease")
        assert r.stored_owner in (None, r.executor), (
            f"{r.label} started on {r.executor} while the item stored {r.stored_owner}'s lease {r.stored_lease}")
    by_item = {}
    for r in sc.runs + sc.batch_runs:
        by_item.setdefault(r.item_id, []).append(r)
    for runs in by_item.values():
        runs.sort(key=lambda r: r.start_ms)
        for a, b in zip(runs, runs[1:]):
            assert (a.end_ms or harness.now_ms()) <= b.start_ms, (
                f"{a.label} ran on {a.executor} and {b.executor} at the same time")
    for t in sc.items.values():
        if t.label in ("Y", "Z"):
            continue
        for r in _executor_runs(sc, t):
            lapses = _lease_lapses(sc, t, r.start_ms, r.end_ms or harness.now_ms(), r.executor)
            assert not lapses, f"{t.label}'s lease was missing, expired or another holder's while its step ran: {lapses[:5]}"


def test_executors_reach_valkey_only_through_valkey_url(sc):
    assert not sc.bypass, f"Valkey connections that did not come through VALKEY_URL: {sc.bypass[:5]}"


def test_queue_is_empty_at_the_end(sc):
    assert not sc.errors or all(not e.startswith("scenario") for e in sc.errors), sc.errors[-1]
    leftover = getattr(sc, "final_items", None)
    assert leftover is not None, "scenario did not finish"
    assert not leftover, f"items left in the queue: {list(leftover)[:5]}"
