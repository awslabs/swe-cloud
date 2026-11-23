"""Live verdict on the destroyer fleet: one run, many independent observations of it."""

import pytest

import harness
from harness import CRASH_BOUND_S, HUNG_BOUND_S, LOGS, N_WORKERS, RUN_DEADLINE_S, STRANDED_GAP_S


@pytest.fixture(scope="module")
def run():
    try:
        result = harness.run_scenario()
    except harness.EnvironmentFailure as failure:
        (LOGS / "environment_failure.txt").write_text(str(failure) + "\n")
        pytest.fail(f"environment: {failure}")
    if not result.get("ran"):
        pytest.fail("; ".join(result["problems"]))
    return result


def attempts(run, vm_id):
    return run["attempts"][vm_id]


def describe(run, vm_id):
    t2 = run["t2"]
    return [
        {"n": a["number"], "start": round(a["started"] - t2, 1), "last_progress": round(a["last_progress"] - t2, 1),
         "end": a["ended"] and round(a["ended"] - t2, 1), "completed": a["completed"], "progress": a["progress"]}
        for a in attempts(run, vm_id)
    ]


def destroyed_by_one_run(run, vm_id):
    """One completed run; or, if the crash fault also killed this VM's run (same worker), one prompt rerun."""
    got = attempts(run, vm_id)
    if len(got) == 1:
        return got[0]["completed"]
    kill = run["crash_kill"]
    if kill is None or len(got) != 2:
        return False
    first, rerun = got
    killed_with_worker = (first["worker_pgid"] == kill["pgid"] and not first["completed"]
                          and first["started"] <= kill["ts"] and (first["ended"] is None or first["ended"] >= kill["ts"]))
    return (killed_with_worker and rerun["completed"]
            and kill["ts"] <= rerun["started"] <= kill["ts"] + CRASH_BOUND_S)


def test_long_teardown_runs_once(run):
    vm_id = run["roles"]["LONG"]
    assert destroyed_by_one_run(run, vm_id), f"long-running VM {vm_id}: {describe(run, vm_id)}"


def test_short_teardowns_run_once(run):
    bad = {vm_id: describe(run, vm_id) for vm_id in run["fillers"] if not destroyed_by_one_run(run, vm_id)}
    assert not bad, f"short VMs not destroyed by exactly one run: {bad}"


def test_dead_workers_destroy_is_finished_by_another(run):
    vm_id = run["roles"]["CRASH"]
    kill = run["crash_kill"]
    assert kill is not None, f"{vm_id}: its first teardown never started ({describe(run, vm_id)})"
    assert kill["pgid"] in run["worker_pids"].values(), (
        f"{vm_id}: vm-destroy's parent was not in a worker's process group (pgid {kill['pgid']}, workers {run['worker_pids']})")
    got = attempts(run, vm_id)
    assert len(got) == 2, f"{vm_id}: expected the killed run and one more, got {describe(run, vm_id)}"
    assert not got[0]["completed"] and got[1]["completed"], f"{vm_id}: {describe(run, vm_id)}"
    delay = got[1]["started"] - kill["ts"]
    assert delay <= CRASH_BOUND_S, f"{vm_id}: restarted {delay:.1f} s after its worker was killed (bound {CRASH_BOUND_S} s)"


def test_hung_teardown_is_taken_over(run):
    vm_id = run["roles"]["HUNG"]
    hang = run["hang"]
    assert hang is not None, f"{vm_id}: its first teardown never reached the hang ({describe(run, vm_id)})"
    got = attempts(run, vm_id)
    assert len(got) >= 2, f"{vm_id}: hung run was never taken over: {describe(run, vm_id)}"
    delay = got[1]["started"] - hang["last_progress"]
    assert delay <= HUNG_BOUND_S, f"{vm_id}: taken over {delay:.1f} s after its last progress line (bound {HUNG_BOUND_S} s)"
    assert len(got) == 2 and got[1]["completed"] and not got[0]["completed"], f"{vm_id}: {describe(run, vm_id)}"


def test_every_vm_destroyed_once_in_time(run):
    t2 = run["t2"]
    problems = {}
    for vm_id, got in run["attempts"].items():
        done = [a for a in got if a["completed"]]
        if len(done) != 1:
            problems[vm_id] = f"{len(done)} completed runs"
        elif done[0]["ended"] - t2 > RUN_DEADLINE_S:
            problems[vm_id] = f"completed {done[0]['ended'] - t2:.0f} s after the requests (deadline {RUN_DEADLINE_S} s)"
        elif any(a["started"] >= done[0]["ended"] for a in got):
            problems[vm_id] = "teardown started again after the VM was destroyed"
    assert not problems, problems


def test_no_concurrent_live_runs(run):
    """No run begins while another run of the same VM is still printing progress."""
    bad = {}
    for vm_id, got in run["attempts"].items():
        for later in got:
            for earlier in got:
                if earlier is later or earlier["started"] > later["started"]:
                    continue
                if earlier["ended"] is not None and earlier["ended"] <= later["started"]:
                    continue
                heard = [ts for ts in earlier["progress_times"] if ts <= later["started"]] or [earlier["started"]]
                if later["started"] - heard[-1] < 3 * harness.PROGRESS_INTERVAL_S:
                    bad[vm_id] = describe(run, vm_id)
    assert not bad, f"a second teardown began while the first was still progressing: {bad}"


def test_teardowns_run_as_worker_children(run):
    pgids = set(run["worker_pids"].values())
    bad = [a for got in run["attempts"].values() for a in got
           if a["worker_pgid"] not in pgids or a["pgid"] != a["worker_pgid"]]
    assert not bad, f"vm-destroy runs outside their worker's process group: {bad[:3]}"


def test_fleet_records_destroyed(run):
    fleet = run.get("fleet")
    assert fleet is not None, run.get("fleet_error")
    bad = {vm_id: fleet.get(vm_id, {}).get("state") for vm_id in run["attempts"] if fleet.get(vm_id, {}).get("state") != "destroyed"}
    assert not bad, f"fleet records not marked destroyed: {bad}"


def test_queue_never_stranded(run):
    samples = run["samples"]
    assert samples, f"no broker samples ({run.get('sampler_errors')})"
    worst, start = 0.0, None
    for ts, messages, consumers in samples:
        if messages > 0 and consumers == 0:
            start = ts if start is None else start
            worst = max(worst, ts - start)
        else:
            start = None
    assert worst <= STRANDED_GAP_S, f"vm.destroy had ready messages and no consumers for {worst:.0f} s"


def test_idle_fleet_subscribed_and_drained(run):
    drain = run["drain"]
    assert drain.get("messages") == 0, f"vm.destroy not empty once the fleet went idle: {drain}"
    assert drain["alive"] == drain["expected"], f"workers exited during the run: {drain}"
    if drain.get("per_worker") is not None:
        missing = [wid for wid in drain["expected"] if drain["per_worker"].get(wid, 0) == 0]
        assert not missing, f"idle workers not subscribed to vm.destroy: {missing} ({drain})"
    else:
        assert drain["consumers"] >= len(drain["expected"]), drain


def test_platform_queue_untouched(run):
    assert run["redeclare_at_publish"] is None, run["redeclare_at_publish"]
    assert run["redeclare_at_end"] is None, run["redeclare_at_end"]


def test_work_stays_on_the_broker(run):
    assert run.get("new_sqs_queues") == [], run.get("new_sqs_queues", run.get("sqs_error"))
