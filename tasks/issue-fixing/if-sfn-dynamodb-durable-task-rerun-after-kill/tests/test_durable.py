"""Verifier for the durable task worker.

One scenario runs once (session fixture); each test below grades one outcome of it, so a failure
in one does not hide the others. Labels: TI1 is the task instance whose tries are killed and whose
first execution is stopped from the console; TI2 is another dag run of the same task whose run_id
differs from TI1's only in ':'/'+' vs '_'; TI3 is a mapped task instance whose last try is killed;
TIL is in flight from the currently deployed version (record and execution already exist).
Next to that timeline run four more task instances, each from a state an earlier try left behind:
TIK's first try was killed before it reached AWS (nothing exists anywhere); TIW's first try, on
the deployed version, was killed after its StartExecution and before it wrote the record (the
execution was started moments ago); TIG's deployed-version try 1 recorded a run that FAILED and its
try 2 died before reaching AWS; TIW2 is TIG plus a try 2 that started another run and was killed
before recording it; TIF's first try, on the deployed version, was killed after its StartExecution
and before it wrote the record, and its job succeeded before Describe/List showed the execution.
TIG and TIW2 need a FAILED execution and only run when the emulator can make one; TIF needs an
execution the verifier can finish on demand.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa: E402

LABELS = ("TI1", "TI2", "TI3", "TIL", "TIK", "TIW", "TIG", "TIW2", "TIF")


@pytest.fixture(scope="session")
def result():
    try:
        return harness.run_scenario()
    except harness.EnvironmentProblem as exc:
        pytest.fail(f"ENVIRONMENT: emulator cannot run the scenario: {exc}", pytrace=False)


def _execs(result, label):
    return [e for e in result["executions"] if e["ti"] == label]


def _attempt(result, label):
    a = result["attempts"].get(label)
    assert a is not None, f"{label} was never run"
    return a


def _brief(execs):
    return [(e["name"], e["status"]) for e in execs]


def test_no_task_instance_ever_has_two_live_executions(result):
    problems = []
    for label in LABELS:
        execs = _execs(result, label)
        def end(e):
            if e["stop"] is not None:
                return e["stop"]
            return float("inf") if e["status"] == "RUNNING" else None

        for i, a in enumerate(execs):
            for b in execs[i + 1:]:
                a_end, b_end = end(a), end(b)
                if None in (a["start"], b["start"], a_end, b_end):
                    continue  # no interval to compare; the RUNNING samples below still apply
                if a["start"] < b_end and b["start"] < a_end:
                    problems.append(f"{label}: {a['name']} and {b['name']} were live at the same time")
        seen = result["running_max"].get(label, 0)
        if seen > 1:
            problems.append(f"{label}: {seen} executions RUNNING at once")
    assert not problems, problems


def test_killed_start_is_picked_up_and_stopped_run_is_started_again(result):
    first = result["first_exec"].get("TI1")
    assert first, "TI1's first try never got an execution started"
    execs = _execs(result, "TI1")
    by_arn = {e["arn"]: e for e in execs}
    assert len(execs) == 2, f"TI1 should end with exactly two executions, has {_brief(execs)}"
    assert by_arn.get(first, {}).get("status") == "ABORTED", f"TI1 executions: {_brief(execs)}"
    other = [e for e in execs if e["arn"] != first]
    assert other[0]["status"] == "SUCCEEDED", f"TI1 executions: {_brief(execs)}"


def test_try_polling_a_stopped_execution_fails(result):
    for label in ("TI1-try2", "TI3-try1"):
        outcome = _attempt(result, label)["outcome"]
        assert isinstance(outcome, int), f"{label} did not exit after its execution was stopped ({outcome})"
        assert outcome != 0, f"{label} reported success for a stopped execution"


def test_try_after_a_kill_reconnects_and_goes_green(result):
    outcome = _attempt(result, "TI1-try4")["outcome"]
    assert outcome == 0, f"TI1-try4 should have finished with the job's success, got {outcome}"


def test_clear_after_success_runs_nothing(result):
    a = _attempt(result, "TI1-try5")
    assert a["outcome"] == 0, f"clearing TI1 after its job succeeded: try exited {a['outcome']}"
    assert not a["forwarded_starts"], f"the clear started {a['forwarded_starts']}"
    before = result["ti1_before_clear"] or []
    after = sorted(e["arn"] for e in _execs(result, "TI1"))
    assert after == before, "the clear changed TI1's executions"


def test_other_dag_run_with_lookalike_run_id_runs_its_own_job(result):
    execs = _execs(result, "TI2")
    assert len(execs) == 1, f"TI2 should have exactly one execution of its own, has {_brief(execs)}"
    assert execs[0]["status"] == "SUCCEEDED", _brief(execs)
    names_ti1 = {e["name"] for e in _execs(result, "TI1")}
    assert execs[0]["name"] not in names_ti1
    outcome = _attempt(result, "TI2-try1")["outcome"]
    assert outcome == 0, f"TI2-try1 exited {outcome}"


def test_in_flight_try_of_the_deployed_version_is_picked_up(result):
    execs = _execs(result, "TIL")
    assert [e["arn"] for e in execs] == result["seeded_by_ti"]["TIL"], (
        f"TIL should keep only the execution it already had, has {_brief(execs)}"
    )
    assert execs[0]["status"] == "SUCCEEDED", _brief(execs)
    outcome = _attempt(result, "TIL-try2")["outcome"]
    assert outcome == 0, f"TIL-try2 exited {outcome}"


def test_reconciler_stops_what_a_dead_last_try_left_running(result):
    last = _attempt(result, "TI3-try2")
    assert last["forwarded_starts"], "TI3's last try never started the job again after the console stop"
    running = result["snapshots"].get("after_reconciler", {}).get("TI3", [])
    assert not running, (
        f"after the reconciler stopped {result['records'].get('TI3', {}).get('execution_arn')}, "
        f"TI3 still has {running} RUNNING"
    )


def test_records_name_the_execution_that_succeeded(result):
    problems = []
    labels = ["TI1", "TI2", "TIL"] + [k for k in ("TIK", "TIW", "TIG", "TIW2") if result["extra_run"].get(k)]
    for label in labels:
        ok = [e["arn"] for e in _execs(result, label) if e["status"] == "SUCCEEDED"]
        recorded = result["records"].get(label, {}).get("execution_arn")
        if len(ok) != 1 or recorded != ok[0]:
            problems.append(f"{label}: record names {recorded}, succeeded executions {ok}")
    assert not problems, problems


def test_every_execution_came_from_a_try_through_its_endpoint(result):
    strays = [e["name"] for e in result["executions"] if e["ti"] in ("unknown", "other")]
    assert not strays, f"executions whose input names no task instance of this scenario: {strays}"
    seeded = set(result["seeded"])
    unproxied = [e["name"] for e in result["executions"] if not e["proxied"] and e["arn"] not in seeded]
    assert not unproxied, f"executions not started through the try's AWS endpoint: {unproxied}"
    for label in ("TI1-try1", "TI1-try3", "TI3-try2"):
        a = _attempt(result, label)
        assert a["fired"], f"{label} never started an execution, so its kill point was never reached"
    assert _attempt(result, "TIK-try1")["fired"], "TIK-try1 never reached AWS, so it was never killed there"


def _green(result, label):
    """The outcome of every try of ``label`` that ran, and whether one of them exited 0."""
    tries = sorted(
        (a for a in result["attempts"].values() if a["ti"] == label), key=lambda a: a["try"]
    )
    return [(a["try"], a["outcome"]) for a in tries], any(a["outcome"] == 0 for a in tries)


def test_task_instance_whose_first_try_died_before_reaching_aws_still_runs(result):
    execs = _execs(result, "TIK")
    tries, green = _green(result, "TIK")
    assert green, f"TIK never went green; tries {tries}, executions {_brief(execs)}"
    assert len(execs) == 1 and execs[0]["status"] == "SUCCEEDED", (
        f"TIK should have run its job exactly once, to success: {_brief(execs)}"
    )


def test_unrecorded_start_of_the_deployed_version_is_picked_up(result):
    problems = []
    for label in ("TIW", "TIW2"):
        if not result["extra_run"].get(label):
            continue
        seeded = result["seeded_by_ti"][label]
        live = seeded[-1]
        execs = _execs(result, label)
        tries, green = _green(result, label)
        if sorted(e["arn"] for e in execs) != sorted(seeded):
            problems.append(f"{label} started a job of its own next to the one already running: {_brief(execs)}")
        by_arn = {e["arn"]: e for e in execs}
        if by_arn.get(live, {}).get("status") != "SUCCEEDED":
            problems.append(f"{label}: the execution its earlier try started did not succeed: {_brief(execs)}")
        if not green:
            problems.append(f"{label} never went green; tries {tries}")
    assert not problems, problems


def test_unrecorded_start_that_succeeded_before_it_was_visible_is_not_run_again(result):
    if not result["extra_run"].get("TIF"):
        pytest.skip("the emulator cannot finish an execution on demand")
    seeded = result["seeded_by_ti"]["TIF"]
    execs = _execs(result, "TIF")
    tries, green = _green(result, "TIF")
    assert sorted(e["arn"] for e in execs) == sorted(seeded), (
        f"TIF's job had already succeeded, but it was started again: {_brief(execs)}"
    )
    assert green, f"TIF never went green; tries {tries}, executions {_brief(execs)}"


def test_run_that_failed_under_the_deployed_version_is_run_again(result):
    if not result["extra_run"].get("TIG"):
        pytest.skip("the emulator cannot make an execution FAILED")
    failed = result["seeded_by_ti"]["TIG"][0]
    execs = _execs(result, "TIG")
    tries, green = _green(result, "TIG")
    assert green, f"TIG never went green; tries {tries}, executions {_brief(execs)}"
    others = [e for e in execs if e["arn"] != failed]
    assert len(others) == 1 and others[0]["status"] == "SUCCEEDED", (
        f"TIG should have run its job once more, to success: {_brief(execs)}"
    )
