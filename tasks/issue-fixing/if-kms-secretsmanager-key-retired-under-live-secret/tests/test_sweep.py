"""Verifier: three scheduled sweeps of one sandbox account, graded on AWS state and the gateway audit."""
import hashlib

import pytest

from scenario import EXIT_INCOMPLETE, RUN_DEADLINE_S, get_scenario


@pytest.fixture(scope="session")
def sc():
    return get_scenario()


def need(sc, *runs):
    if not sc.environment_ok:
        pytest.fail(sc.error)
    for run in runs:
        if run not in sc.runs:
            pytest.fail(f"{run} did not take place; scenario error:\n{sc.error}")


def describe_run(sc, run):
    r = sc.runs[run]
    return f"{run}: rc={r['rc']} killed={r['killed']} elapsed={r['elapsed']:.1f}s\n--- log tail ---\n{r['log_tail']}"


def check_exit(sc, run, expected):
    r = sc.runs[run]
    assert not r["killed"], f"sweep still running after {RUN_DEADLINE_S}s and was killed\n" + describe_run(sc, run)
    assert r["rc"] == expected, f"expected exit {expected}\n" + describe_run(sc, run)


def check_states(sc, run, expected):
    states = sc.snapshots[run]["keys"]
    wrong = {k: states[k] for k, want in expected.items() if states[k] != want}
    assert not wrong, f"after {run}: key states {wrong}, expected {expected}"


def check_intact(sc, run, labels):
    for label in labels:
        con = sc.consumers[label]
        view = sc.snapshots[run]["consumers"].get(label)
        assert view is not None, f"after {run}: {con.cid} (encrypted under {con.key}) no longer exists"
        assert "error" not in view, f"after {run}: could not inspect {con.cid}: {view['error']}"
        assert not view.get("scheduled_for_deletion"), f"after {run}: {con.cid} was scheduled for deletion"
        assert view["bytes_intact"] and view["ref_intact"], f"after {run}: {con.cid} was modified"
        assert view["decrypts"], (f"after {run}: {con.cid} can no longer be decrypted under {con.key}: "
                                  f"{view.get('decrypt_error')}")


def check_removed_with_backup(sc, run, labels):
    snap = sc.snapshots[run]
    for label in labels:
        con = sc.consumers[label]
        assert snap["consumers"].get(label) is None, f"after {run}: {con.cid} still exists"
        want = hashlib.sha256(con.plaintext).hexdigest()
        records = snap["backups"].get(con.cid, [])
        assert any(rec.get("plaintext_sha256") == want for rec in records), (
            f"after {run}: {con.cid} was deleted without a backup of its plaintext (records: {records})")


# -- run 1: S1 protected, S2 throttled, kept bucket unreadable at first, a late writer -----------

def test_run1_stops_and_reports(sc):
    need(sc, "run1")
    check_exit(sc, "run1", EXIT_INCOMPLETE)


def test_run1_keys_still_in_use_stay_enabled(sc):
    need(sc, "run1")
    check_states(sc, "run1", {"K1": "Enabled", "K3": "Enabled", "K4": "Enabled"})


def test_run1_surviving_consumers_still_readable(sc):
    need(sc, "run1")
    check_intact(sc, "run1", ["S1", "O4", "O5"])
    tags = sc.snapshots["run1"]["consumers"]["S1"]["tags"]
    assert tags.get("DeletionProtection") == "true", f"deletion protection on S1 was changed: {tags}"


def test_run1_unused_keys_retired_in_the_same_sweep(sc):
    need(sc, "run1")
    check_states(sc, "run1", {"K2": "PendingDeletion", "K5": "PendingDeletion"})


def test_run1_removed_consumers_backed_up(sc):
    need(sc, "run1")
    check_removed_with_backup(sc, "run1", ["O1", "S2", "O2", "O2B"])


# -- run 2: protection lifted ---------------------------------------------------------------

def test_run2_stops_and_reports(sc):
    need(sc, "run2")
    check_exit(sc, "run2", EXIT_INCOMPLETE)


def test_run2_retires_keys_freed_since_last_sweep(sc):
    need(sc, "run2")
    check_removed_with_backup(sc, "run2", ["S1", "O5"])
    check_states(sc, "run2", {"K1": "PendingDeletion", "K3": "PendingDeletion"})


def test_run2_kept_consumer_keeps_its_key(sc):
    need(sc, "run2")
    check_states(sc, "run2", {"K4": "Enabled"})
    check_intact(sc, "run2", ["O4"])


# -- run 3: the kept object is gone --------------------------------------------------------

def test_run3_completes(sc):
    need(sc, "run3")
    check_exit(sc, "run3", 0)
    check_states(sc, "run3", {"K1": "PendingDeletion", "K2": "PendingDeletion", "K3": "PendingDeletion",
                              "K4": "PendingDeletion", "K5": "PendingDeletion"})


# -- audit ---------------------------------------------------------------------------------

def _key_label(sc, ref):
    for label, k in sc.keys.items():
        if ref in (k["id"], k["arn"]):
            return label
    if ref.startswith("alias/") or ":alias/" in ref:
        try:
            key_id = sc.c["kms"].describe_key(KeyId=ref)["KeyMetadata"]["KeyId"]
        except Exception:
            return None
        return _key_label(sc, key_id)
    return None


def _schedules(sc):
    out = []
    for e in sc.gateway.audit:
        if e.get("service") == "kms" and e.get("action") == "ScheduleKeyDeletion" and e.get("status") == 200:
            label = _key_label(sc, e.get("resource", ""))
            if label:
                out.append((label, e))
    return out


def _deletions(sc):
    """consumer label -> monotonic time its deletion completed (sweep via gateway, or its owner)."""
    by_cid = {con.cid: label for label, con in sc.consumers.items()}
    done = {}
    for e in sc.gateway.audit:
        status = e.get("status", 0)
        if e.get("service") == "secretsmanager" and e.get("action") == "DeleteSecret" and status == 200:
            cids = [e.get("resource")]
        elif e.get("action") == "DeleteObject" and status in (200, 204):
            cids = [f"s3://{e.get('bucket')}/{e.get('key')}"]
        elif e.get("action") == "DeleteObjects" and status == 200:
            cids = [f"s3://{e.get('bucket')}/{k}" for k in e.get("deleted_keys", [])]
        else:
            continue
        for cid in cids:
            label = by_cid.get(cid)
            if label and label not in done:
                done[label] = (e["t_end"], e)
    for label, con in sc.consumers.items():
        if con.removed_by_owner_ns and label not in done:
            done[label] = (con.removed_by_owner_ns, None)
    return done


def test_no_key_scheduled_while_something_encrypted_under_it_existed(sc):
    need(sc, "run1")
    deletions = _deletions(sc)
    problems = []
    for key, e in _schedules(sc):
        for label, con in sc.consumers.items():
            if con.key != key or con.created_ns is None or con.created_ns >= e["t_start"]:
                continue
            gone = deletions.get(label)
            if gone is None or gone[0] >= e["t_start"]:
                problems.append(f"{e['run']}: ScheduleKeyDeletion({key}) while {con.cid} still existed")
    assert not problems, "\n".join(problems)


def test_changes_went_through_the_gateway(sc):
    need(sc, "run1")
    final = sc.snapshots[max(sc.snapshots)]
    scheduled = {key for key, _ in _schedules(sc)}
    deletions = _deletions(sc)
    problems = []
    for key, state in final["keys"].items():
        if state != "Enabled" and key not in scheduled:
            problems.append(f"{key} is {state} without a ScheduleKeyDeletion through the gateway")
    for label, view in final["consumers"].items():
        if view is None and label not in deletions:
            problems.append(f"{sc.consumers[label].cid} is gone without a delete through the gateway")
    o4 = deletions.get("O4")
    if o4 is not None and o4[1] is not None:
        problems.append(f"the sweep deleted {sc.consumers['O4'].cid}, which the config keeps")
    s1 = deletions.get("S1")
    if s1 is not None and s1[1] is not None and s1[1].get("run") == "run1":
        problems.append(f"{sc.consumers['S1'].cid} was deleted while deletion protection was on")
    assert not problems, "\n".join(problems)


def test_pass_pauses_announced(sc):
    need(sc, "run1")
    seen = [m.get("pass_no") for m in sc.runs["run1"]["seams"] if m.get("seam") == "pass_start"]
    assert 1 in seen and 2 in seen, f"run1 announced pass_start for passes {seen}; expected every pass"
    assert not sc.runs["run1"]["seam_errors"], sc.runs["run1"]["seam_errors"]
