"""Grade the table lock on real storage state and the gateway's record of every lock write.

Two scenarios run once each (module fixtures): a clean run with two contending workers, and a
faulted run that adds a flaky gateway in front of the ingest worker and a compaction sidecar that
locks under the same owner. Each check below is its own test so one failure never hides another.
"""
import os
import random
import string
import uuid

import pytest

from rig.gateway import norm_etag
from rig.runner import (Scenario, list_ledger_entries, list_lock_versions, read_json)

LEASE_SECONDS = 20
DRIFT_BUFFER_MS = 500
HOLD_MS = 300
POLL_MS = 100
ACQUIRE_TIMEOUT_S = 15
SIDECAR_HOLD_MS = 1500
RELEASE_FAIL_COUNT = 2
GATE_TIMEOUT_S = 8
JOIN_TIMEOUT_S = 150
RECOVERY_DEADLINE_S = 5
LEASE_SHORT_TOLERANCE_MS = 5000
LEASE_LONG_TOLERANCE_MS = 1000


def _suffix(n=6):
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def _common(bucket):
    return dict(bucket=bucket, lock_key="orders/_locks/table_lock.json",
                ledger_prefix="orders/ledger/", results_prefix="orders/results/",
                lease_seconds=LEASE_SECONDS, drift_buffer_ms=DRIFT_BUFFER_MS,
                hold_ms=HOLD_MS, poll_ms=POLL_MS, acquire_timeout_s=ACQUIRE_TIMEOUT_S,
                sidecar_hold_ms=SIDECAR_HOLD_MS, release_fail_count=RELEASE_FAIL_COUNT,
                gate_timeout_s=GATE_TIMEOUT_S)


@pytest.fixture(scope="module")
def clean_run():
    s2 = random.choice([3, 4])
    bucket = f"lakehouse-clean-{_suffix()}"
    owners = {"w1": f"orders-ingest-{_suffix(4)}", "w2": f"orders-backfill-{_suffix(4)}"}
    ports = {"w1": 4571, "w2": 4572}
    sc = Scenario(owners=owners, sections={"w1": 4, "w2": s2}, faults=False, ports=ports,
                  **_common(bucket))
    sc.__enter__()
    try:
        out = sc.run(JOIN_TIMEOUT_S)
    finally:
        sc.__exit__(None, None, None)   # free the ports before the next scenario
    yield dict(sc=sc, out=out, total=4 + s2, owners=owners, s2=s2)


@pytest.fixture(scope="module")
def faulted_run():
    s2 = random.choice([3, 4])
    bucket = f"lakehouse-orders-{_suffix()}"
    owners = {"w1": f"orders-ingest-{_suffix(4)}", "w2": f"orders-backfill-{_suffix(4)}"}
    ports = {"w1": 4581, "w2": 4582}
    sc = Scenario(owners=owners, sections={"w1": 4, "w2": s2}, faults=True, ports=ports,
                  **_common(bucket))
    sc.__enter__()
    try:
        out = sc.run(JOIN_TIMEOUT_S)
    finally:
        sc.__exit__(None, None, None)
    yield dict(sc=sc, out=out, total=4 + 2 + s2, owners=owners, s2=s2)


# --------------------------------------------------------------------------- helpers

def _producer_of(chain, seed_etag, etag):
    if etag == seed_etag:
        return ("seed", True, 0)
    for w in chain:
        if w.etag == etag:
            return (w.author, w.expired, w.valid_until)
    return (None, None, None)


def _ledger(bucket, prefix):
    rows = [doc for _, _, doc in list_ledger_entries(bucket, prefix)]
    return rows


def _results(bucket, prefix, worker, sections):
    return {s: read_json(bucket, f"{prefix}{worker}-{s}.json") for s in range(1, sections + 1)}


# --------------------------------------------------------------------------- clean run

def test_clean_workers_exit_ok(clean_run):
    out = clean_run["out"]
    for role, (rc, so, se) in out.items():
        assert rc == 0, f"{role} did not exit cleanly (rc={rc}); stderr tail:\n{se[-2000:]}"


def test_clean_ledger_is_a_clean_sequence(clean_run):
    sc, total = clean_run["sc"], clean_run["total"]
    rows = _ledger(sc.cfg["bucket"], sc.cfg["ledger_prefix"])
    seqs = sorted(r["seq"] for r in rows)
    assert seqs == list(range(1, total + 1)), f"ledger seqs {seqs}, expected 1..{total}"
    counter = read_json(sc.cfg["bucket"], f"{sc.cfg['ledger_prefix']}counter.json")
    assert counter["seq"] == total


def test_clean_no_overlap(clean_run):
    sc = clean_run["sc"]
    _assert_mutual_exclusion(sc)


def test_clean_no_dangling(clean_run):
    sc = clean_run["sc"]
    _assert_no_dangling(sc)


def test_clean_all_acquired_and_released(clean_run):
    sc, s2 = clean_run["sc"], clean_run["s2"]
    for worker, n in (("w1", 4), ("w2", s2)):
        res = _results(sc.cfg["bucket"], sc.cfg["results_prefix"], worker, n)
        for s, r in res.items():
            assert r and r["acquired"] and r["released"], f"{worker}-{s} result {r}"


# --------------------------------------------------------------------------- faulted run: completion

def test_phases_all_fired(faulted_run):
    sc = faulted_run["sc"]
    missing = [p for p in ("P1", "P2", "P3", "P4", "P5", "L1", "L2") if p not in sc.rig.phases]
    assert not missing, (f"fault phases never reached: {missing}; the worker did not progress "
                         f"through its sections. contract issues: {sc.rig.contract_violations}")


def test_faulted_workers_exit_ok(faulted_run):
    for role, (rc, so, se) in faulted_run["out"].items():
        assert rc == 0, f"{role} did not exit cleanly (rc={rc}); stderr tail:\n{se[-2000:]}"


def test_faulted_ledger_is_a_clean_sequence(faulted_run):
    sc, total = faulted_run["sc"], faulted_run["total"]
    rows = _ledger(sc.cfg["bucket"], sc.cfg["ledger_prefix"])
    seqs = sorted(r["seq"] for r in rows)
    assert seqs == list(range(1, total + 1)), (
        f"ledger seqs {seqs}, expected 1..{total} (duplicate or lost seq => overlap)")
    counter = read_json(sc.cfg["bucket"], f"{sc.cfg['ledger_prefix']}counter.json")
    assert counter["seq"] == total


def test_faulted_ledger_counter_never_went_backwards(faulted_run):
    # Every counter write that reached storage, in the order it landed (the gateway's and the
    # sidecar's records). The counter is the last committed sequence number: a write that lands
    # over a later commit's value loses that commit, whoever made it and whenever it was sent.
    sc = faulted_run["sc"]
    landed = [(a, seq, t) for a, seq, t in sc.rig.counter_writes if seq is not None]
    high = None
    for author, seq, t in landed:
        assert high is None or seq >= high[1], (
            f"the ledger counter went back from {high[1]} (written by {high[0]}) to {seq} "
            f"(written by {author}, landed at t={t:.1f}s); a commit's counter write reached "
            f"storage after a later commit had already advanced it")
        if high is None or seq > high[1]:
            high = (author, seq)
    late = sc.rig.phases.get("L1", {})
    assert "late_status" in late, "a held ledger write was never resolved by the gateway"


# --------------------------------------------------------------------------- faulted run: safety

def test_faulted_mutual_exclusion(faulted_run):
    _assert_mutual_exclusion(faulted_run["sc"])


def test_faulted_ledger_entries_under_own_live_lease(faulted_run):
    sc = faulted_run["sc"]
    chain, seed = sc.rig.chain, sc.rig.seed_etag
    for key, _, doc in list_ledger_entries(sc.cfg["bucket"], sc.cfg["ledger_prefix"]):
        worker = doc["worker"]
        if worker == "compactor":
            continue
        etag = norm_etag(doc.get("lock_etag"))
        author, expired, _ = _producer_of(chain, seed, etag)
        assert author == worker and expired is False, (
            f"ledger entry {key} committed under lock version {etag} authored by {author} "
            f"(expired={expired}), not a live lease held by {worker}")


def test_faulted_sidecar_never_lost_its_lease(faulted_run):
    sc = faulted_run["sc"]
    assert not sc.rig.sidecar.lease_stolen, (
        "the compaction sidecar's own release was rejected (412): another writer adopted or "
        "expired the sidecar's live lease")


def test_faulted_no_dangling(faulted_run):
    _assert_no_dangling(faulted_run["sc"])


# --------------------------------------------------------------------------- faulted run: liveness

def test_P1_landed_acquire_adopted(faulted_run):
    sc = faulted_run["sc"]
    ea = sc.rig.phases["P1"]["etag"]
    p1_epoch = sc.rig.phases["P1"]["epoch"]
    rows = [(lm, doc) for _, lm, doc in list_ledger_entries(sc.cfg["bucket"], sc.cfg["ledger_prefix"])
            if doc["worker"] == "w1" and doc["section"] == 1]
    assert rows, "ingest never committed its first section after its acquire answer was lost"
    lm, doc = rows[0]
    assert norm_etag(doc["lock_etag"]) == ea, (
        f"ingest's first commit is under {doc['lock_etag']}, not the version its own acquire "
        f"landed ({ea}); it stranded its own lease instead of adopting it")
    dt = lm.timestamp() * 1000 - p1_epoch
    assert dt <= RECOVERY_DEADLINE_S * 1000 + 1500, (
        f"ingest recovered its lost acquire only after {dt/1000:.1f}s "
        f"(> {RECOVERY_DEADLINE_S}s); it waited out its own lease")


def test_P2_release_retried_until_it_landed(faulted_run):
    sc = faulted_run["sc"]
    ea = sc.rig.phases["P1"]["etag"]
    p2_epoch = sc.rig.phases["P2"]["epoch"]
    # An expired version authored by w1, conditional on its held acquisition (Ea).
    rel = [w for w in sc.rig.chain if w.author == "w1" and w.expired and w.if_match == ea]
    assert rel, "ingest never landed the release of its first section (dangling release)"
    dt = rel[0].epoch_ms - p2_epoch
    assert dt <= RECOVERY_DEADLINE_S * 1000 + 1500, f"release landed only after {dt/1000:.1f}s"
    r = read_json(sc.cfg["bucket"], f"{sc.cfg['results_prefix']}w1-1.json")
    assert r and r["released"], f"ingest reported its first section unreleased: {r}"


def test_P5_landed_release_reported_released(faulted_run):
    sc = faulted_run["sc"]
    r = read_json(sc.cfg["bucket"], f"{sc.cfg['results_prefix']}w1-4.json")
    assert r and r["acquired"] and r["released"], (
        f"ingest's last section result is {r}; a release that landed behind a dropped "
        f"connection was reported as a failure")


def test_faulted_all_sections_acquired(faulted_run):
    sc, s2 = faulted_run["sc"], faulted_run["s2"]
    for worker, n in (("w1", 4), ("w2", s2)):
        res = _results(sc.cfg["bucket"], sc.cfg["results_prefix"], worker, n)
        for s, rr in res.items():
            assert rr and rr["acquired"], f"{worker}-{s} never acquired the lock: {rr}"


def test_faulted_acquire_leases_are_well_formed(faulted_run):
    # The lease a worker grants itself is measured from when the gateway received the write
    # (the gateway may hold a write before forwarding it). The whole-second lease end makes a
    # correct body up to ~1 s short; a bounded retry backoff may add a little more.
    sc = faulted_run["sc"]
    for w in sc.rig.chain:
        if w.author in ("w1", "w2") and not w.expired and w.recv_ms is not None:
            granted = w.valid_until - w.recv_ms
            assert LEASE_SECONDS * 1000 - LEASE_SHORT_TOLERANCE_MS <= granted <= \
                LEASE_SECONDS * 1000 + LEASE_LONG_TOLERANCE_MS, (
                f"{w.author} acquire grants a {granted} ms lease (validUntil={w.valid_until}); "
                f"the table's lease is {LEASE_SECONDS}s and must not be shortened or stretched")


# --------------------------------------------------------------------------- faulted run: provenance

def test_faulted_contract_respected(faulted_run):
    sc = faulted_run["sc"]
    assert not sc.rig.contract_violations, f"lock contract violations: {sc.rig.contract_violations}"


def test_faulted_every_lock_version_is_attributable(faulted_run):
    sc = faulted_run["sc"]
    versions = list_lock_versions(sc.cfg["bucket"], sc.cfg["lock_key"])
    known = {sc.rig.seed_etag} | {w.etag for w in sc.rig.chain}
    unknown = [(etag, body) for etag, body in versions if etag not in known]
    assert not unknown, (
        f"lock versions not produced through the gateway or the sidecar: {unknown}; "
        f"a worker bypassed the storage gateway")


# --------------------------------------------------------------------------- shared assertions

def _assert_mutual_exclusion(sc):
    chain, seed = sc.rig.chain, sc.rig.seed_etag
    for w in chain:
        author, expired, valid_until = _producer_of(chain, seed, w.if_match)
        if author is None:
            continue
        if author != w.author and expired is False:
            live = w.epoch_ms <= valid_until + DRIFT_BUFFER_MS
            assert not live, (
                f"{w.author} wrote over lock version {w.if_match} authored by {author}, "
                f"which was still a live lease (validUntil={valid_until}) at the write")


def _assert_no_dangling(sc):
    final_body = read_json(sc.cfg["bucket"], sc.cfg["lock_key"])
    assert final_body is not None, "lock object missing"
    assert final_body.get("expired") is True, f"the lock was left un-expired: {final_body}"
    # In this workload no process dies, so a correct holder always releases (writes expired=true)
    # before its lease lapses. A different writer taking over a non-expired version whose lease had
    # already run out therefore means someone stranded its own lease — the dangling-lock symptom.
    chain, seed = sc.rig.chain, sc.rig.seed_etag
    for w in chain:
        author, expired, valid_until = _producer_of(chain, seed, w.if_match)
        if author is None or expired is not False or w.author == author:
            continue
        lapsed = w.epoch_ms > valid_until + DRIFT_BUFFER_MS
        assert not lapsed, (
            f"{w.author} took over lock version {w.if_match} (owner lease authored by {author}) "
            f"only after it had dangled past its lease end (validUntil={valid_until})")
