"""Scale-up under faults: every invocation's SQSBatchResponse against the runner state at return,
and the end state once the build queue has drained.

Each scenario is run once (lazily, on first use) against its own queue, DLQ, SSM path, fleet and
code-host stand-ins; a scenario that cannot run fails its own tests only.
"""

from __future__ import annotations

import json

import pytest

import harness

_RUNS: dict[str, object] = {}


def run_of(name: str) -> harness.Run:
    if name not in _RUNS:
        try:
            _RUNS[name] = harness.Run(name).execute()
        except Exception as error:  # noqa: BLE001
            _RUNS[name] = error
    outcome = _RUNS[name]
    if isinstance(outcome, Exception):
        pytest.fail(f"scenario {name} could not be run: {outcome!r}")
    return outcome


SCENARIOS = list(harness.SCENARIOS)


@pytest.mark.parametrize("name", SCENARIOS)
def test_batch_response_matches_runner_state(name):
    """Listed = no configured runner for the job at return; unlisted = has one (or is ignored)."""
    run = run_of(name)
    assert run.invocations, "the scale-up function was never invoked"
    problems = []
    for inv in run.invocations:
        if inv.listed is None:
            problems.append(
                f"invocation {inv.n}: {inv.outcome}; Lambda redelivers the whole batch "
                f"({run.describe_jobs(r['messageId'] for r in inv.records)}) although jobs "
                f"{sorted(j for j in inv.configured_jobs if j in {r['job_id'] for r in inv.records})} already had configured runners"
            )
            continue
        listed, expected = set(inv.listed), set(inv.expected)
        lost = expected - listed
        extra = listed - expected
        if lost:
            problems.append(f"invocation {inv.n}: not returned for retry although no configured runner exists: {run.describe_jobs(lost)}")
        if extra:
            ignored = {r["messageId"] for r in inv.records if r["ignored"]}
            problems.append(
                f"invocation {inv.n}: returned for retry although the job already has a configured runner"
                f"{' or the event is ignored by design' if extra & ignored else ''}: {run.describe_jobs(extra)}"
            )
    assert not problems, "\n".join(problems) + f"\n(seed {run.seed}; logs in {run.log_dir})"


@pytest.mark.parametrize("name", SCENARIOS)
def test_every_job_gets_exactly_one_runner(name):
    """Over the whole run each job gets one configured runner: none lost, none doubled."""
    run = run_of(name)
    problems = []
    for job in run.scenario.jobs:
        mine = [i for i in run.fleet.instances.values() if i.job_id == job.job_id and not i.seeded]
        if job.ignored:
            if mine:
                problems.append(f"job {job.job_id} (repository owned by a User, ignored by design) got runner instance(s) {[i.instance_id for i in mine]}")
            continue
        configured = [i for i in mine if run.configured_ever(i)]
        if not configured:
            problems.append(f"job {job.job_id} ({job.owner}) never got a configured runner: it is lost")
        elif len(configured) > 1:
            problems.append(
                f"job {job.job_id} ({job.owner}) got {len(configured)} configured runners: "
                + ", ".join(f"{i.instance_id} (invocation {i.invocation})" for i in configured)
            )
    assert not problems, "\n".join(problems) + f"\n(seed {run.seed})"


@pytest.mark.parametrize("name", SCENARIOS)
def test_no_unconfigured_instance_left_running(name):
    run = run_of(name)
    orphans = [i for i in run.fleet.running() if not run.configured(i, run.final_ssm)]
    assert not orphans, (
        "running instances without a runner config once the queue drained (they hold cap slots): "
        + ", ".join(f"{i.instance_id} (job {i.job_id}, {i.owner}, launched in invocation {i.invocation})" for i in orphans)
    )


@pytest.mark.parametrize("name", SCENARIOS)
def test_runner_cap_never_exceeded(name):
    run = run_of(name)
    over = {owner: peak for owner, peak in run.fleet.peak.items() if peak > harness.RUNNERS_MAXIMUM_COUNT}
    assert not over, (
        f"running runner instances per owner peaked above RUNNERS_MAXIMUM_COUNT={harness.RUNNERS_MAXIMUM_COUNT}: {over}; "
        + "; ".join(f"{owner}: {', '.join(run.fleet.peak_at.get(owner, []))}" for owner in over)
    )


@pytest.mark.parametrize("name", SCENARIOS)
def test_build_queue_drained_without_dead_letters(name):
    run = run_of(name)
    assert not run.unresolved, "; ".join(run.unresolved)
    assert sum(run.final_queue.values()) == 0, f"build queue not empty at the end: {run.final_queue}"
    assert sum(run.final_dlq.values()) == 0, f"{run.final_dlq} message(s) ended in the DLQ"
    assert not run.foreign, (
        "messages the verifier never sent appeared on the build queue (the function's role has no "
        f"sqs:SendMessage; see iam_role in deploy/scale-up.json): {run.foreign[:3]}"
    )


@pytest.mark.parametrize("name", SCENARIOS)
def test_deployment_contract_kept(name):
    """Queue settings untouched; runner configs written through the configured SSM endpoint."""
    run = run_of(name)
    attributes = run.final_attributes
    assert attributes.get("VisibilityTimeout") == str(harness.VISIBILITY_TIMEOUT_S), attributes
    assert "FifoQueue" not in attributes or attributes["FifoQueue"] == "false", attributes
    redrive = json.loads(attributes.get("RedrivePolicy", "{}"))
    assert str(redrive.get("maxReceiveCount")) == str(harness.MAX_RECEIVE_COUNT), redrive
    assert redrive.get("deadLetterTargetArn") == run.dlq_arn, redrive
    bypassed = [name for name, value in run.final_ssm.items() if run.proxy.forwarded_puts.get(name) != value]
    assert not bypassed, f"SSM parameters not written through AWS_ENDPOINT_URL_SSM: {bypassed}"
