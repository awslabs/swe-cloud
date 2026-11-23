"""One run of four pools against one Valkey: two pools SIGKILLed while holding jobs (one pod whole, as a
deploy does, and one pod's pool process alone, as the memory guard does, leaving its 20 s runner going),
a 40 s job held throughout, and connection resets on a live pool's requests. Each test grades one
property of that run."""

import contextlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rig as R  # noqa: E402


@pytest.fixture(scope="module")
def run():
    result = R.run_rig()
    if result.environment_error:
        os.makedirs("/logs/verifier", exist_ok=True)
        with open("/logs/verifier/environment.txt", "w") as marker:
            marker.write(result.environment_error + "\n")
    with contextlib.suppress(OSError):
        os.makedirs("/logs/verifier", exist_ok=True)
        with open("/logs/verifier/run.txt", "w") as summary:
            summary.write(result.describe() + "\n")
            if result.error:
                summary.write(result.error + "\n")
            if getattr(result, "observer", None):
                summary.write(f"max executing: {result.observer.max_executing}\n")
            summary.write(f"final state: {result.final_state}\n")
            summary.write("\n".join(result.notes[:20]) + "\n")
    return result


def _context(run) -> str:
    tails = "\n".join(f"--- {role} stderr tail ---\n{pool.stderr_tail(1500)}" for role, pool in run.pools.items())
    return f"{run.describe()}\n{tails}"


def _require_run(run):
    if run.environment_error:
        pytest.fail(f"environment: {run.environment_error}")
    if run.error:
        pytest.fail(f"the run did not complete its timeline:\n{run.error}\n{_context(run)}")


def test_pools_start_and_survive_connection_errors(run):
    _require_run(run)
    assert not run.unexpected_exits, f"pools exited without being signalled: {run.unexpected_exits}\n{_context(run)}"


def test_faults_reached_the_pool(run):
    _require_run(run)
    unfired = [f for f in run.faults if f["graded"] and f["state"] != "fired"]
    assert not unfired, (
        f"{len(unfired)} scheduled connection resets on P2 never fired: a release fault needs P2 to finish a job "
        "and take it out of its in-progress list, the claim fault a reply telling P2 about a job just moved to "
        f"its in-progress list (a pool that stops finishing jobs never reaches the later ones):\n{_context(run)}"
    )


def test_max_concurrency_never_exceeded(run):
    _require_run(run)
    obs = run.observer
    assert obs.max_executing <= R.MAX_CONCURRENCY, (
        f"{obs.max_executing} jobs executed at once (MaxConcurrency {R.MAX_CONCURRENCY}); first over-admission: "
        f"{obs.over_admissions[:1]}\n{_context(run)}"
    )


def test_lock_never_below_running_jobs(run):
    _require_run(run)
    bad = []
    for t, s1, lock, _llen, info, s2 in run.samples:
        running = len(s1 & s2)
        if lock < running or lock > R.MAX_CONCURRENCY:
            bad.append((round(t - run.t0, 2), lock, running, info))
    assert not bad, (
        f"{len(bad)} samples where {R.KEY_LOCK} was below the jobs executing at that moment or above "
        f"MaxConcurrency; first: (t, lock, executing, lock_info) = {bad[:3]}\n{_context(run)}"
    )


def test_capacity_comes_back(run):
    _require_run(run)
    longest, worst = run.longest_phantom()
    assert longest <= R.RECLAIM_DEADLINE_S, (
        f"{R.KEY_LOCK} counted units no executing job held, with jobs waiting, for {longest:.1f}s "
        f"(from, to, units) = {worst}; capacity must be back within {R.RECLAIM_DEADLINE_S}s\n{_context(run)}"
    )


def test_every_job_runs(run):
    _require_run(run)
    obs = run.observer
    missing = [j for j in run.job_ids if j not in obs.ends]
    assert not missing, (
        f"{len(missing)} of {len(run.job_ids)} jobs never completed within {R.RUN_DEADLINE_S}s of the backlog: "
        f"{missing[:5]}\n{_context(run)}"
    )
    again = {j: n for j, n in obs.starts.items() if n > 1 and j not in run.killed_jobs}
    assert not again, (
        f"jobs ran more than once although the deploy never killed a runner of theirs (it killed "
        f"{sorted(run.killed_jobs)}): {again}\n{_context(run)}"
    )


def test_runners_only_die_with_their_pod(run):
    _require_run(run)
    cut = run.cut_off_runs()
    assert not cut, (
        f"{len(cut)} runners went away mid-job though only the deploy-killed pod's runners "
        f"({sorted(run.killed_jobs)}) may; first (t, job, pool): {cut[:3]}\n{_context(run)}"
    )


def test_valkey_state_settles(run):
    _require_run(run)
    state = run.final_state
    assert state is not None, _context(run)
    assert run.quiesced, (
        f"within {R.QUIESCE_S}s of the last job: expected lock 0, every lock_info field 0, every in-progress "
        f"list empty and worker_pools = {{P2, P3}}; found {state}\n{_context(run)}"
    )
