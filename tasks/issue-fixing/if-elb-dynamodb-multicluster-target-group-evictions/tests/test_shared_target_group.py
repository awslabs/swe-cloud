"""Graded run: two cluster controllers and hand-registered hosts share one ELBv2 target group while
controllers are killed and their egress path drops responses.

The schedule runs once; each test grades one observation of it, so every failure is reported.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import harness  # noqa: E402
from harness import A, B, M, CLUSTERS  # noqa: E402

_RESULT = {}


@pytest.fixture(scope="module")
def run():
    if "res" not in _RESULT:
        try:
            _RESULT["res"] = harness.run_schedule()
        except Exception as exc:  # every test reports the crash instead of a missing score
            _RESULT["res"] = exc
    res = _RESULT["res"]
    if isinstance(res, Exception):
        pytest.fail(f"schedule could not run: {res!r}")
    return res


def _owner(run, target, ts):
    return harness.owner_at(run["handoffs"], target, ts)


def _phase(run, name):
    phase = run["phases"].get(name)
    assert phase is not None, f"{name} never ran"
    return phase


def _assert_converged(phase, name):
    assert phase.get("ok"), (
        f"{name}: target group never held the expected set within the deadline: "
        f"missing={phase.get('missing')} unexpected={phase.get('unexpected')}")


def _proxy_entries(run, cluster, action=None, service=None, forwarded=True):
    for entry in run["proxy"][cluster]:
        if forwarded and not entry["forwarded"]:
            continue
        if action is not None and entry["action"] != action:
            continue
        if service is not None and entry["service"] != service:
            continue
        yield entry


# ---- convergence after each perturbation ---------------------------------------------------


def test_p1_clusters_and_legacy_hosts_coexist(run):
    _assert_converged(_phase(run, "P1"), "P1")


def test_p2_target_registered_before_crash_is_removed_after_restart(run):
    phase = _phase(run, "P2")
    assert phase["fired"], "cluster-a never sent RegisterTargets for its new endpoint"
    _assert_converged(phase, "P2")


def test_p2_ownership_item_exists_when_registration_lands(run):
    phase = _phase(run, "P2")
    assert phase["fired"], "cluster-a never sent RegisterTargets for its new endpoint"
    at_kill = run["ledger_at_kill"]
    missing = sorted(set(at_kill["targets"]) - set(at_kill["rows"]))
    assert not missing, (
        f"RegisterTargets for {at_kill['targets']} was applied by ELB while tgb_ownership had no "
        f"cluster-a item for {missing}")


def test_p3_lost_deregistration_is_completed_after_restart(run):
    phase = _phase(run, "P3")
    assert phase["fired"], "cluster-a never sent DeregisterTargets for its removed endpoint"
    _assert_converged(phase, "P3")


def test_p4_registration_with_lost_responses_does_not_leak(run):
    phase = _phase(run, "P4")
    assert phase["fired"], "cluster-a never sent RegisterTargets for its new endpoint"
    _assert_converged(phase, "P4")


def test_p5_address_handed_to_other_cluster_is_left_registered(run):
    phase = _phase(run, "P5")
    assert phase["fired"], "cluster-a never sent DeregisterTargets for its removed endpoint"
    handoff = next(h for h in run["handoffs"] if h["target"] == A[16])
    gone = [round(ts, 2) for ts, present, _ in run["samples"]
            if present is not None and handoff["ts"] <= ts <= phase["end_ts"] and A[16] not in present]
    deregs = [round(e["ts"], 2) for e in _proxy_entries(run, "cluster-a", action="DeregisterTargets")
              if A[16] in e["targets"] and handoff["ts"] <= e["ts"]]
    assert not deregs and not gone, (
        f"{A[16]} belonged to a cluster-b pod from {round(handoff['ts'], 2)}, but cluster-a "
        f"deregistered it at {deregs[:5]} and it was missing from the group at {gone[:5]}")
    _assert_converged(phase, "P5")


def test_p6_stale_binding_snapshot_after_agent_failover_is_not_applied(run):
    phase = _phase(run, "P6")
    assert phase["fired"], "cluster-a never sent RegisterTargets for its new endpoint"
    # From just after the new pod's registration was applied until the phase ends, every pod in
    # cluster-a's current binding stays in the group, also while the file holds the older snapshot.
    start = phase["registered_ts"] + 1.0
    gone = sorted({(t, round(ts, 2)) for ts, present, _ in run["samples"]
                   if present is not None and start <= ts <= phase["end_ts"]
                   for t in phase["a_endpoints"] if t not in present})
    assert not gone, (
        f"cluster-a's binding generation {phase['generation']} lists {phase['a_endpoints']}; after "
        f"the standby agent wrote its older snapshot (generation {phase['stale_generation']}) these "
        f"live pods were missing from the group: {gone[:5]}")
    _assert_converged(phase, "P6")


def test_r1_stale_snapshot_after_handoff_leaves_other_clusters_pod_alone(run):
    phase = _phase(run, "R1")
    target = phase["target"]
    handoff = next(h for h in run["handoffs"] if h["target"] == target)
    gone = [round(ts, 2) for ts, present, _ in run["samples"]
            if present is not None and handoff["ts"] <= ts <= phase["end_ts"] and target not in present]
    deregs = [round(e["ts"], 2) for e in _proxy_entries(run, "cluster-a", action="DeregisterTargets")
              if target in e["targets"] and handoff["ts"] <= e["ts"] <= phase["end_ts"]]
    assert not deregs and not gone, (
        f"{target} belonged to a cluster-b pod from {round(handoff['ts'], 2)}. cluster-a restarted "
        f"on the standby agent's older snapshot (generation {phase['stale_generation']}, which still "
        f"lists it) while cluster-b's controller was down, then got generation "
        f"{phase['generation']}: cluster-a deregistered it at {deregs[:5]} and it was missing from "
        f"the group at {gone[:5]}")
    _assert_converged(phase, "R1")


def test_r2_address_that_came_back_is_registered_after_restart(run):
    # While cluster-a was down the address went to a cluster-b pod and back to a new cluster-a pod.
    _assert_converged(_phase(run, "R2"), "R2")


def test_p7_deleted_binding_removes_only_its_targets(run):
    phase = _phase(run, "P7")
    assert phase["fired"], "cluster-b never removed anything after its binding was deleted"
    handoff = next(h for h in run["handoffs"] if h["target"] == B[17])
    gone = [round(ts, 2) for ts, present, _ in run["samples"]
            if present is not None and ts >= handoff["ts"] and B[17] not in present]
    assert not gone, (
        f"{B[17]} belonged to a cluster-a pod from {round(handoff['ts'], 2)} but was missing from "
        f"the group at {gone[:5]}")
    _assert_converged(phase, "P7")


def test_p7_deleted_binding_clears_ownership_and_exits_zero(run):
    phase = _phase(run, "P7")
    assert phase["rows_b"] == [], f"cluster-b items left in tgb_ownership: {phase['rows_b']}"
    assert phase["exit_code"] == 0, f"cluster-b controller exit status {phase['exit_code']} (expected 0)"


# ---- safety across the whole run -----------------------------------------------------------


def test_no_controller_deregisters_a_target_it_does_not_own(run):
    bad = []
    for cluster in CLUSTERS:
        for entry in _proxy_entries(run, cluster, action="DeregisterTargets"):
            foreign = [t for t in entry["targets"]
                       if _owner(run, t, entry["ts"]) not in (None, cluster)]
            if foreign:
                bad.append((cluster, round(entry["ts"], 2), foreign))
    assert not bad, f"DeregisterTargets of targets owned by someone else: {bad[:10]}"


def _presence_runs(samples):
    """Per target: list of (start_ts, end_ts or None, vanished_at_ts or None)."""
    runs, current, last_ts = {}, {}, None
    for ts, present, _ in samples:
        if present is None:
            continue
        for t in present:
            if t not in current:
                current[t] = ts
        for t in list(current):
            if t not in present:
                runs.setdefault(t, []).append((current.pop(t), last_ts, ts))
        last_ts = ts
    for t, start in current.items():
        runs.setdefault(t, []).append((start, last_ts, None))
    return runs


def test_targets_only_disappear_when_their_owner_deregisters_them(run):
    runs = _presence_runs(run["samples"])
    bad = []
    for target, spans in runs.items():
        for start, _end, vanished in spans:
            if vanished is None:
                continue
            owner = _owner(run, target, vanished)
            if owner is None:
                continue
            if owner == "manual":
                bad.append((target, "legacy host removed", round(vanished, 2)))
                continue
            regs = [e["ts"] for e in _proxy_entries(run, owner, action="RegisterTargets")
                    if target in e["targets"] and e["ts"] <= start]
            since = max(regs) if regs else start - 1.0
            deregs = [e for e in _proxy_entries(run, owner, action="DeregisterTargets")
                      if target in e["targets"] and since < e["ts"] <= vanished]
            if not deregs:
                bad.append((target, f"vanished without a {owner} deregistration", round(vanished, 2)))
    assert not bad, f"targets removed by someone other than their owner: {bad[:10]}"


def test_legacy_hosts_present_throughout(run):
    samples = [s for s in run["samples"] if s[1] is not None]
    assert samples, "no target group samples recorded"
    missing = []
    for target, registered_at in run["manual_registered"].items():
        seen = False
        for ts, present, _ in samples:
            if ts < registered_at:
                continue
            if target in present:
                seen = True
            elif seen:
                missing.append((target, round(ts, 2)))
                break
    assert not missing, f"legacy hosts missing from the group: {missing}"


def test_controller_traffic_goes_through_its_egress_endpoint(run):
    bad = []
    runs = _presence_runs(run["samples"])
    for target, spans in runs.items():
        for start, _end, _vanished in spans:
            owner = _owner(run, target, start)
            if owner not in CLUSTERS:
                continue
            regs = [e for e in _proxy_entries(run, owner, action="RegisterTargets")
                    if target in e["targets"] and e["ts"] <= start]
            if not regs:
                bad.append((target, "registered without a RegisterTargets through its egress"))
    written = {c: set() for c in CLUSTERS}
    for cluster in CLUSTERS:
        for e in _proxy_entries(run, cluster, service="dynamodb"):
            if e["mutating"]:
                written[cluster].update(e["targets"])
    snaps = list(run["ledger_snapshots"])
    if run.get("ledger_at_kill"):
        snaps.append({"rows": {"cluster-a": run["ledger_at_kill"]["rows"]}})
    for snap in snaps:
        for cluster, rows in snap["rows"].items():
            for row in rows:
                if row not in written[cluster]:
                    bad.append((row, f"{cluster} item written without a call through its egress"))
    assert not bad, f"AWS calls that bypassed the egress endpoint: {sorted(set(bad))[:10]}"


def test_ownership_table_matches_each_clusters_targets(run):
    every = set(A.values()) | set(B.values()) | set(M.values())
    bad = []
    for snap in run["ledger_snapshots"]:
        listed = set(snap["listed"])
        for cluster in CLUSTERS:
            mine = {t for t in every if _owner(run, t, snap["ts"]) == cluster}
            rows = set(snap["rows"][cluster])
            uncovered = sorted((listed & mine) - rows)
            if uncovered:
                bad.append((snap["label"], cluster, "in the group without an item", uncovered))
            foreign = sorted(t for t in rows if _owner(run, t, snap["ts"]) != cluster)
            if foreign:
                bad.append((snap["label"], cluster, "items naming targets it does not own", foreign))
    assert not bad, f"tgb_ownership disagrees with the target group: {bad}"


def test_target_descriptions_follow_egress_policy(run):
    rejected = [(c, e["action"], e.get("reason")) for c in CLUSTERS for e in run["proxy"][c]
                if e["rejected"]]
    assert not rejected, f"requests rejected by egress policy: {rejected[:10]}"
