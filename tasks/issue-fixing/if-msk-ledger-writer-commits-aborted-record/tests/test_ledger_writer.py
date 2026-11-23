"""Verifier for the ledger writer. Each run is a fresh topic and transactional id on one MSK cluster."""

from __future__ import annotations

import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import harness  # noqa: E402

EXIT_FENCED = 3

_SEED = random.SystemRandom().randrange(1 << 30)
_runs: dict[str, harness.Run] = {}
_errors: dict[str, BaseException] = {}


@pytest.fixture(scope="session")
def bootstrap():
    try:
        return harness.ensure_cluster()
    except harness.EnvironmentNotLive as exc:
        print(f"ENVIRONMENT: {exc}")
        pytest.fail(f"ENVIRONMENT: {exc}")


def _run(kind: str, bootstrap: str) -> harness.Run:
    if kind in _errors:
        pytest.fail(f"run {kind} could not be executed: {_errors[kind]!r}")
    if kind not in _runs:
        try:
            _runs[kind] = harness.execute(kind, bootstrap, _SEED + len(_runs) + hash(kind) % 1000)
        except Exception as exc:  # noqa: BLE001
            _errors[kind] = exc
            raise
        print(_runs[kind].describe())
    return _runs[kind]


def _ledger_problems(run: harness.Run) -> str:
    expected, got = run.expected_entries(), run.committed_entries()
    missing = expected - got
    extra = got - expected
    if not missing and not extra:
        return ""
    dupes = {k: n for k, n in got.items() if n > 1}
    return (f"committed ledger differs from the input batches: missing={sorted(missing)[:6]} "
            f"extra={sorted(extra)[:6]} duplicated={dict(list(dupes.items())[:6])}")


def _exit(run: harness.Run, listener: str):
    return next(w.code for w in run.writers.values() if w.listener == listener)


def _writer(run: harness.Run, listener: str) -> harness.Writer:
    return next(w for w in run.writers.values() if w.listener == listener)


def _assert_recovery_exit(run: harness.Run) -> None:
    """A writer the standby replaced exits fenced; one that finished before the standby started still owned the id."""
    a, b = _writer(run, "A"), _writer(run, "B")
    if a.ended is not None and a.ended <= b.started:
        assert a.code == 0, "the writer finished before the standby started but did not exit 0\n" + run.describe()
    else:
        assert a.code == EXIT_FENCED, "the replaced writer did not exit as fenced\n" + run.describe()


# ── control: no fault ────────────────────────────────────────────────────────────────


def test_control_run_commits_every_entry_once(bootstrap):
    run = _run("control", bootstrap)
    assert _exit(run, "A") == 0, run.describe()
    assert not run.violations, run.describe()
    problems = _ledger_problems(run)
    assert not problems, problems + "\n" + run.describe()
    assert run.lso == run.hwm, f"last stable offset {run.lso} behind high watermark {run.hwm}\n" + run.describe()


# ── a produce request that times out on the writer and reaches the broker later ──────────


def test_timeout_writer_rides_through(bootstrap):
    run = _run("timeout", bootstrap)
    assert run.hold_time is not None, "the writer never sent the scheduled entry " + str(run.hold_entry) + "\n" + run.describe()
    a = run.writers[next(n for n, w in run.writers.items() if w.listener == "A")]
    assert a.code == 0, "the writer did not finish its batches after the delivery timeout\n" + run.describe()
    assert a.ended - a.started <= harness.RUN_DEADLINE_S, run.describe()
    assert not run.violations, run.describe()


def test_timeout_recovery_is_prompt(bootstrap):
    run = _run("timeout", bootstrap)
    assert run.hold_time is not None, run.describe()
    later = [t for t in run.commit_times("A") if t > run.hold_time]
    assert later, "no transaction committed after the delivery timeout\n" + run.describe()
    budget = harness.DELIVERY_TIMEOUT_S + harness.RECOVERY_BUDGET_S
    assert later[0] - run.hold_time <= budget, (
        f"first commit after the stalled request came {later[0] - run.hold_time:.1f}s after it (budget {budget}s)\n"
        + run.describe())


def test_timeout_ledger_exactly_once(bootstrap):
    run = _run("timeout", bootstrap)
    assert run.hold_time is not None, run.describe()
    assert run.replay is not None, "verifier error: the stalled request was never delivered\n" + run.describe()
    problems = _ledger_problems(run)
    assert not problems, problems + "\n" + run.describe()


def test_timeout_partition_not_left_open(bootstrap):
    run = _run("timeout", bootstrap)
    assert run.lso == run.hwm, f"last stable offset {run.lso} behind high watermark {run.hwm}\n" + run.describe()


# ── the standby takes over while the active writer is recovering from a timeout ──────────


def test_takeover_during_recovery_exit_codes(bootstrap):
    run = _run("timeout_takeover", bootstrap)
    assert run.hold_time is not None, run.describe()
    assert len(run.writers) == 2, "verifier error: standby never started\n" + run.describe()
    assert _exit(run, "B") == 0, "the standby that took over did not finish\n" + run.describe()
    _assert_recovery_exit(run)
    assert not run.violations, run.describe()


def test_takeover_during_recovery_ledger_exactly_once(bootstrap):
    run = _run("timeout_takeover", bootstrap)
    problems = _ledger_problems(run)
    assert not problems, problems + "\n" + run.describe()
    assert run.lso == run.hwm, f"last stable offset {run.lso} behind high watermark {run.hwm}\n" + run.describe()


# ── the active's recovery reaches the coordinator while the standby has a transaction open ──


def test_takeover_mid_standby_transaction_exit_codes(bootstrap):
    run = _run("timeout_takeover_inside", bootstrap)
    assert run.hold_time is not None, run.describe()
    assert len(run.writers) == 2, "verifier error: standby never started\n" + run.describe()
    assert _exit(run, "B") == 0, "the standby that took over did not finish\n" + run.describe()
    _assert_recovery_exit(run)
    assert not run.violations, run.describe()


def test_takeover_mid_standby_transaction_ledger_exactly_once(bootstrap):
    run = _run("timeout_takeover_inside", bootstrap)
    problems = _ledger_problems(run)
    assert not problems, problems + "\n" + run.describe()
    assert run.lso == run.hwm, f"last stable offset {run.lso} behind high watermark {run.hwm}\n" + run.describe()


# ── an ordinary takeover between two batches ────────────────────────────────────────────


def test_plain_takeover(bootstrap):
    run = _run("takeover", bootstrap)
    assert len(run.writers) == 2, run.describe()
    assert _exit(run, "B") == 0, run.describe()
    assert _exit(run, "A") == EXIT_FENCED, run.describe()
    assert not run.violations, run.describe()
    problems = _ledger_problems(run)
    assert not problems, problems + "\n" + run.describe()
