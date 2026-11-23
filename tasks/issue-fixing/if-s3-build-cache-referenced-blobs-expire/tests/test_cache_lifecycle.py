"""Verifier: one staged run of two cachesvc replicas against a lifecycle-bounded bucket.

Each test grades one property of the same run, so every failure is reported.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rig  # noqa: E402

LATENCY_MAX = 0.8          # disk-hit FindMissing reply (OPERATIONS.md: p99 < 250 ms)
LATENCY_EXCEPTIONS = 2
HOT_REQUESTS_MAX = 20      # meter-seen requests on the hot blob's key during phase 1
HOT_WRITES_MAX = 9         # successful PUT + COPY of the hot blob during phase 1
PREMISE_MAX = rig.MARGIN + 0.5
AC_READ_SLACK = 2.0
LEASE_SLACK = 1.0          # around lifecycle's deletion of the hot blob

_RUN = {}


@pytest.fixture(scope="session")
def run():
    if "result" not in _RUN:
        reason = rig.preflight()
        if reason:
            _RUN["result"] = {"environment": reason}
        else:
            _RUN["result"] = rig.run_scenario()
    res = _RUN["result"]
    if "environment" in res:
        pytest.fail(f"ENVIRONMENT: {res['environment']}")
    if res.get("setup_error"):
        pytest.fail(f"run could not start: {res['setup_error']}")
    return res


def _events(res, tag=None, replica=None):
    return [e for e in res["events"]
            if (tag is None or e["tag"] == tag) and (replica is None or e["replica"] == replica)]


def _lm_values(res, key):
    rec = res["history"].get(key)
    return sorted(rec["lms"]) if rec else []


def _last_seen(res, key):
    rec = res["history"].get(key)
    return rec["last_seen"] if rec else None


def _xkey(res):
    return rig.cas_key(res["X"])


# --- the service contract -------------------------------------------------------------------

def test_contract_and_restart(run):
    problems = []
    t0 = _events(run, "t0_findmissing")
    if not t0 or t0[0]["status"] != 200 or \
            {d["hash"] for d in (t0[0].get("reply") or {}).get("missing", [])} != {run["X"], run["Y"]}:
        problems.append(f"initial findmissing for two new blobs: {t0[:1]}")
    for c in run["checks"]:
        if c["ack"] != 200 or not c["present"]:
            problems.append(f"{c['what']} {c['key']}: ack {c['ack']}, in bucket {c['present']}")
    for e in _events(run, "hot") + _events(run, "burst") + _events(run, "lease"):
        if e["status"] != 200:
            problems.append(f"{e['replica']} findmissing at t={e['t_start']}: status {e['status']}")
    for e in _events(run, "r2_fetch"):
        if e["status"] != 200:
            problems.append(f"r2 GET /cas of the hot blob at t={e['t_start']}: status {e['status']}")
    if not run["restarts"] or not all(r["ready"] for r in run["restarts"]):
        problems.append(f"r1 did not come back healthy after restart: {run['restarts']}")
    if run.get("driver_errors"):
        problems.append(f"driver errors: {run['driver_errors']}")
    if run["janitor_errors"]:
        problems.append(f"lifecycle could not delete: {run['janitor_errors'][:5]}")
    assert not problems, "\n".join(problems[:20])


# --- B: no AC entry outlives its outputs ----------------------------------------------------

def test_no_ac_entry_references_a_deleted_blob(run):
    assert not run["dangling"], (
        f"{len(run['dangling'])} observations of an ac/ entry in the bucket whose output was not: "
        f"{run['dangling'][:5]}")


def test_outputs_young_enough_when_ac_written(run):
    """At each AC write, every output's age in S3 must be within cas.v2 expiry - ac expiry."""
    bad = []
    keys = [rig.ac_key(a) for a in run["A"]] + [rig.ac_key(run["B"])]
    outputs = {rig.ac_key(a): rig.cas_key(run["X"]) for a in run["A"]}
    outputs[rig.ac_key(run["B"])] = rig.cas_key(run["Y"])
    for key in keys:
        lms = _lm_values(run, key)
        if not lms:
            bad.append(f"{key}: never observed in the bucket")
            continue
        l_ac = lms[0]
        cas_lms = _lm_values(run, outputs[key])
        # With whole-second LastModified, an output rewritten in the same second counts as newer.
        tol = 1.0 if run["lm_whole_seconds"] else 0.0
        before = [v for v in cas_lms if v <= l_ac + tol]
        if not before:
            bad.append(f"{key} written at t={l_ac:.1f}: its output had no version yet")
            continue
        age = max(0.0, l_ac - before[-1])
        if age > PREMISE_MAX:
            bad.append(f"{key} written at t={l_ac:.1f}: output {outputs[key]} last modified "
                       f"t={before[-1]:.1f}, age {age:.1f}s > {rig.MARGIN:.0f}s")
    assert not bad, "\n".join(bad)


def test_other_replica_serves_hot_entry_with_its_output(run):
    p = run.get("probe")
    assert p is not None, "probe did not run"
    assert p["status"] == 200, f"r2 GET /ac of the newest hot entry at t={p['t']:.1f}: {p['status']}"
    assert p["cas_present"], f"t={p['t']:.1f}: r2 serves the entry but its output is not in the bucket"


# --- A: cost, AC immutability, reclamation --------------------------------------------------

def test_lease_checks_find_what_the_cache_has(run):
    """A build without the bytes cannot re-upload: a false 'missing' or an error is a lost input."""
    xk = _xkey(run)
    uploaded = [c["t"] for c in run["checks"] if c["what"] == "cas_after_ack" and c["key"] == xk
                and c["present"]]
    deleted = [t for t, _ in run["janitor_deleted"].get(xk, [])]
    gone_at = min(deleted) if deleted else float("inf")
    bad = []
    for e in _events(run, "lease"):
        if not uploaded:
            continue
        in_bucket = uploaded[0] < e["t_start"] and e["t_end"] < gone_at - LEASE_SLACK
        if not in_bucket:
            continue
        if e["status"] != 200:
            bad.append(f"{e['replica']} t={e['t_start']:.1f}: status {e['status']}")
            continue
        missing = {d.get("hash") for d in (e.get("reply") or {}).get("missing", [])
                   if isinstance(d, dict)}
        if run["X"] in missing:
            bad.append(f"{e['replica']} t={e['t_start']:.1f}: reported missing")
    assert not bad, (
        f"{len(bad)} lease checks for the hot blob failed while it was on the replica's disk and "
        f"in the bucket (a build without the bytes fails on that): {bad[:12]}")


def test_disk_hits_answer_without_waiting_on_s3(run):
    hits = [e for e in _events(run, "hot") + _events(run, "burst") + _events(run, "lease")
            if e["status"] == 200]
    slow = [(e["t_start"], e["latency"]) for e in hits if e["latency"] > LATENCY_MAX]
    assert len(slow) <= LATENCY_EXCEPTIONS, (
        f"{len(slow)} of {len(hits)} findmissing replies for a blob on local disk took over "
        f"{LATENCY_MAX}s: {slow[:10]}")


def test_hot_blob_s3_traffic_is_bounded(run):
    """r1 serves the hot blob from disk all through phase 1; count what that costs in S3."""
    xk = _xkey(run)
    reqs = [m for m in run["meter"] if m["key"] == xk and m.get("via") == "r1"
            and 0 <= m["t_in"] < rig.PHASE1_END]
    writes = [m for m in reqs if m["op"] in ("PUT", "COPY") and m["status"] and
              200 <= m["status"] < 300]
    by_op = {}
    for m in reqs:
        by_op[m["op"]] = by_op.get(m["op"], 0) + 1
    assert len(reqs) <= HOT_REQUESTS_MAX, (
        f"{len(reqs)} S3 requests from r1 on the hot blob in {rig.PHASE1_END:.0f}s "
        f"(cas_refresh_age {rig.REFRESH_AGE:.0f}s): {by_op}")
    assert len(writes) <= HOT_WRITES_MAX, (
        f"hot blob written by r1 {len(writes)} times in {rig.PHASE1_END:.0f}s: "
        f"{[round(m['t_in'], 1) for m in writes]}")


def test_ac_entries_are_never_refreshed(run):
    problems = []
    copies = [m for m in run["meter"] if m["op"] == "COPY" and m["key"].startswith("ac/")]
    if copies:
        problems.append(f"{len(copies)} copies onto ac/ keys, first at t={copies[0]['t_in']:.1f}")
    driver_puts = {}
    for e in _events(run, "put_ac"):
        k = rig.ac_key(e["path"].rsplit("/", 1)[1])
        driver_puts[k] = driver_puts.get(k, 0) + 1
    meter_puts = {}
    for m in run["meter"]:
        if m["op"] == "PUT" and m["key"].startswith("ac/"):
            meter_puts[m["key"]] = meter_puts.get(m["key"], 0) + 1
    for k, n in meter_puts.items():
        if n > driver_puts.get(k, 0):
            problems.append(f"{k}: {n} uploads for {driver_puts.get(k, 0)} client writes")
    for k in driver_puts:
        lms = _lm_values(run, k)
        if len(lms) > 1:
            problems.append(f"{k}: LastModified moved after the client wrote it: {lms}")
    assert not problems, "\n".join(problems[:20])


def test_unused_objects_are_reclaimed(run):
    problems = []
    end = run["t_end"]
    for e in _events(run, "put_ac"):
        k = rig.ac_key(e["path"].rsplit("/", 1)[1])
        last = _last_seen(run, k)
        if last is not None and last - e["t_end"] > rig.AC_RECLAIM:
            problems.append(f"{k} written t={e['t_end']:.1f}, still in the bucket at t={last:.1f}")

    def last_reference(h):
        times = [e["t_end"] for e in run["events"] if h in (e.get("names") or [])
                 and (e["tag"] not in ("p2_get_ac", "probe", "lease_get_ac") or e["status"] == 200)]
        return max(times) if times else 0.0

    for label, h in (("cold blob", run["Y"]), ("hot blob", run["X"])):
        k = rig.cas_key(h)
        deadline = last_reference(h) + rig.CAS_RECLAIM
        last = _last_seen(run, k)
        if last is None:
            continue
        if last > deadline:
            problems.append(f"{label} {k}: last requested t={last_reference(h):.1f}, still in the "
                            f"bucket at t={last:.1f} (must be gone by t={deadline:.1f}; run ended "
                            f"t={end:.1f})")
    assert not problems, "\n".join(problems[:20])


def test_ac_reads_follow_the_bucket(run):
    stale = []
    deleted = run["janitor_deleted"]
    for e in _events(run, "p2_get_ac", "r2"):
        if e["status"] != 200:
            continue
        k = rig.ac_key(e["path"].rsplit("/", 1)[1])
        gone = [t for t, _ in deleted.get(k, [])]
        if gone and min(gone) < e["t_start"] - AC_READ_SLACK:
            stale.append(f"{k}: served at t={e['t_start']:.1f}, removed by lifecycle at "
                         f"t={min(gone):.1f}")
    assert not stale, "\n".join(stale)


def test_all_bucket_writes_went_through_the_service_endpoint(run):
    writes = {}
    for m in run["meter"]:
        if m["op"] in ("PUT", "COPY") and m["status"] and 200 <= m["status"] < 300:
            writes[m["key"]] = writes.get(m["key"], 0) + 1
    bad = []
    for key, rec in run["history"].items():
        if len(rec["lms"]) > writes.get(key, 0):
            bad.append(f"{key}: {len(rec['lms'])} versions observed, {writes.get(key, 0)} writes "
                       f"through S3_ENDPOINT")
    assert not bad, "\n".join(bad[:20])
