"""Fault-injection trials for fenced_worker.

Every trial runs worker-1 (holder) and worker-2 (standby) from /app, each
behind its own proxy, on a fresh lock table and ledger bucket, drives one
fault schedule, then audits DynamoDB (item + stream), every ledger version in
S3, the proxy logs and both journals.
"""

import os
import random
import signal
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness as H  # noqa: E402

REPEATS = 2
CASES = [(c, r) for c in ("A1", "A2", "B1", "B2", "C", "R") for r in range(REPEATS)]


def test_emulator_live():
    H.aws("dynamodb").list_tables()
    H.aws("s3", s3={"addressing_style": "path"}).list_buckets()


# --------------------------------------------------------------------------- live phases


def wait_takeover_and_entry(t):
    def ok():
        item = t.lock_item()
        if not item or not (item["owner"] or "").startswith("worker-2:") or item["fence"] != 2:
            return None
        entries = t.ledger_now().get("entries", [])
        return any(e.get("writer") == "worker-2" and e.get("fence") == 2 for e in entries)
    return t.poll(ok, H.B_HOLD_MAX_MS / 1000.0)


def run_a(t):
    p1 = t.proxies["worker-1"]
    with p1.mu:
        p1.drop_next_update = "reset" if t.case == "A1" else "500"
    if not p1.fired.wait(H.CAPTURE_TIMEOUT_MS / 1000.0):
        t.fail("worker-1 sent no lock heartbeat through its endpoint within %d ms"
               % H.CAPTURE_TIMEOUT_MS)
        return
    t.ctx["fault_at"] = p1.fire_info["rec"]["t_fwd"]
    t.ctx["n0"] = len(t.ledger_now().get("entries", []))
    time.sleep(H.A_OBSERVE_MS / 1000.0)
    t.ctx["observed"] = True


def run_b1(t):
    p1 = t.proxies["worker-1"]
    with p1.mu:
        p1.capture_put = True
    if not p1.fired.wait(H.CAPTURE_TIMEOUT_MS / 1000.0):
        t.fail("worker-1 sent no ledger PutObject through its endpoint within %d ms"
               % H.CAPTURE_TIMEOUT_MS)
        return
    t.ctx["captured_at"] = p1.fire_info["rec"]["t_recv"]
    if not wait_takeover_and_entry(t):
        t.fail("worker-2 did not take over and append a fence-2 entry within %d ms of worker-1 "
               "losing DynamoDB" % H.B_HOLD_MAX_MS)
    with p1.mu:
        p1.capture_put = False
        held = list(p1.held_puts)
    t.ctx["released_at"] = t.now()
    p1.release_puts.set()
    t.poll(lambda: all(r.get("t_resp") is not None for r in held), 10)
    time.sleep(H.B_POST_RELEASE_MS / 1000.0)
    t.ctx["observed"] = True


def run_b2(t):
    p1 = t.proxies["worker-1"]
    pid = t.workers["worker-1"].proc.pid

    def on_capture(kind, rec):
        if kind == "capture_get":
            with p1.mu:
                p1.ddb_blackhole = True
            os.kill(pid, signal.SIGSTOP)

    p1.on_capture = on_capture
    with p1.mu:
        p1.capture_get = True
    if not p1.fired.wait(H.CAPTURE_TIMEOUT_MS / 1000.0):
        t.fail("worker-1 sent no ledger GetObject through its endpoint within %d ms"
               % H.CAPTURE_TIMEOUT_MS)
        return
    t.ctx["captured_at"] = p1.fire_info["rec"]["t_recv"]
    if not wait_takeover_and_entry(t):
        t.fail("worker-2 did not take over and append a fence-2 entry within %d ms of worker-1 "
               "freezing" % H.B_HOLD_MAX_MS)
    t.ctx["released_at"] = t.now()
    p1.release_get.set()
    p1.get_delivered.wait(10)
    os.kill(pid, signal.SIGCONT)
    time.sleep(H.B_POST_RELEASE_MS / 1000.0)
    t.ctx["observed"] = True


def run_c(t):
    p1 = t.proxies["worker-1"]
    with p1.mu:
        p1.after_drop = "c_mode"
        p1.drop_next_update = "reset"
    if not p1.fired.wait(H.CAPTURE_TIMEOUT_MS / 1000.0):
        t.fail("worker-1 sent no lock heartbeat through its endpoint within %d ms"
               % H.CAPTURE_TIMEOUT_MS)
        return
    s = p1.fire_info["rec"]["t_fwd"]
    t.ctx["s"] = s
    item = t.lock_item()
    t.ctx["T"] = item["rvn"] if item else None
    if not item or item["owner"] != t.w1_owner:
        t.fail("lock item not held by worker-1 right after its heartbeat landed: %s" % item)
    remaining = s + H.C_OBSERVE_MS / 1000.0 - t.now()
    time.sleep(max(0.0, remaining))
    t.ctx["observed"] = True


def wait_holding_and_appended(t, wid, timeout_s):
    """Until `wid` holds the lock and has an entry with that fence in the ledger."""
    def ok():
        item = t.lock_item()
        if not item or not (item["owner"] or "").startswith(wid + ":"):
            return None
        entries = t.ledger_now().get("entries", [])
        return item if any(e.get("writer") == wid and e.get("fence") == item["fence"]
                           for e in entries) else None
    return t.poll(ok, timeout_s)


def run_r(t):
    """Rolling deploy: SIGTERM the holder, then the new holder after a restart.

    During the first handover the brownout costs worker-2 the response to its
    acquiring write (applied, connection reset)."""
    t.ctx["handovers"] = []
    p2 = t.proxies["worker-2"]
    with p2.mu:
        p2.drop_ops = ("PutItem", "UpdateItem")
        p2.drop_next_update = "reset"
    t_sig = t.sigterm("worker-1")
    t.ctx["handovers"].append((t_sig, "worker-2"))
    if not wait_holding_and_appended(t, "worker-2", H.R_WAIT_MS / 1000.0):
        t.fail("worker-2 did not hold the lock and append within %d s of worker-1's SIGTERM"
               % (H.R_WAIT_MS // 1000))
    t.ctx["dropped"] = p2.fired.is_set()
    with p2.mu:
        p2.drop_next_update = None
    time.sleep(H.R_SETTLE_MS / 1000.0)
    t.reap("worker-1")
    w1b = t.start_worker("worker-1")
    if not w1b.ready.wait(H.READY_TIMEOUT_MS / 1000.0):
        t.fail("restarted worker-1 did not print READY within %ds" % (H.READY_TIMEOUT_MS // 1000))
        return
    time.sleep(1.0 + random.random())
    t_sig = t.sigterm("worker-2")
    t.ctx["handovers"].append((t_sig, "worker-1"))
    if not wait_holding_and_appended(t, "worker-1", H.R_WAIT_MS / 1000.0):
        t.fail("the restarted worker-1 did not hold the lock and append within %d s of worker-2's "
               "SIGTERM" % (H.R_WAIT_MS // 1000))
    time.sleep(H.R_SETTLE_MS / 1000.0)
    t.ctx["observed"] = True


RUNNERS = {"A1": run_a, "A2": run_a, "B1": run_b1, "B2": run_b2, "C": run_c, "R": run_r}


# --------------------------------------------------------------------------- audits


def check_common(t):
    epochs, violations = t.epochs()
    for v in violations:
        t.fail(v)
    entries = t.audit_ledger(epochs)
    return epochs, entries


def check_a(t):
    epochs, entries = check_common(t)
    item = t.snapshot.get("item")
    if not item or item["owner"] != t.w1_owner or item["fence"] != 1:
        t.fail("worker-1 lost a lock it still owned: after its heartbeat was applied but reported "
               "failed, the lock item ended as %s (expected owner %s, fence 1)" % (item, t.w1_owner))
    owners = [o for o, _ in epochs]
    if any(o != t.w1_owner for o in owners):
        t.fail("the lock changed hands during the trial: %s" % epochs)
    if entries is not None and "n0" in t.ctx:
        after = [e for e in entries[t.ctx["n0"]:]
                 if e.get("writer") == "worker-1" and e.get("fence") == 1]
        t.notes.append("worker-1 appended %d entries after the fault" % len(after))
        if len(after) < H.A_MIN_ENTRIES_AFTER_FAULT:
            t.fail("worker-1 appended only %d entries in the %d s after the fault (expected at least %d)"
                   % (len(after), H.A_OBSERVE_MS // 1000, H.A_MIN_ENTRIES_AFTER_FAULT))


def check_handover(t, epochs, last_hb):
    item = t.snapshot.get("item")
    if not item or not (item["owner"] or "").startswith("worker-2:") or item["fence"] != 2:
        t.fail("expected worker-2 to hold the lock with fence 2 at the end, found %s" % item)
    if len(epochs) != 2 or epochs[0] != (t.w1_owner, 1) or not (epochs[1][0] or "").startswith("worker-2:"):
        t.fail("expected exactly one handover worker-1 (fence 1) -> worker-2 (fence 2), saw %s" % epochs)
    t.check_takeover_timing(last_hb)


def check_b(t):
    epochs, _entries = check_common(t)
    takeover = t.takeover_time()
    last_hb = t.last_landed_heartbeat(takeover if takeover is not None else float("inf"))
    check_handover(t, epochs, last_hb)


def check_c(t):
    epochs, _entries = check_common(t)
    s, T = t.ctx.get("s"), t.ctx.get("T")
    if s is None or T is None:
        return
    check_handover(t, epochs, s)
    if t.stream_records is not None and len(epochs) == 2:
        old = t.epoch_old_rvn.get(epochs[1])
        if old is not None and old != T:
            t.fail("worker-2 took over from version tag %s, not from the one worker-1's last "
                   "applied heartbeat wrote" % old)
    lo = s + H.C_RESOLVE_DELAY_MS / 1000.0
    hi = s + H.LEASE_MS / 1000.0
    renewals = []
    for r in t.proxy_log("worker-1"):
        if r["kind"] == "ddb" and r["lock_table"] and r["op"] == "UpdateItem" and lo <= r["t_recv"] < hi:
            has_clause, refs = H.condition_refs_rvn_value(r.get("body", {}), T)
            renewals.append((has_clause, refs))
    reads = [r["t_recv"] - s for r in t.proxy_log("worker-1")
             if r["kind"] == "ddb" and r["lock_table"] and r["op"] in H.DDB_READ_OPS
             and r.get("status") == 200 and r["t_recv"] >= s]
    t.notes.append("first answered worker-1 lock read at s+%s; heartbeats in window: %s"
                   % ("%.2f" % reads[0] if reads else "never", renewals))
    if not any(refs or not has_clause for has_clause, refs in renewals):
        t.fail("worker-1's heartbeat had been applied and its lease was still running, but once "
               "DynamoDB reads answered again (%.1f s after that heartbeat) it sent no heartbeat "
               "against the version tag stored in the item before the lease ran out (%d heartbeat "
               "attempts in that window)" % (H.C_RESOLVE_DELAY_MS / 1000.0, len(renewals)))


def check_r(t):
    epochs, _entries = check_common(t)
    owners = []
    for o, _ in epochs:
        if not owners or owners[-1] != o:
            owners.append(o)
    wids = [(o or "").split(":")[0] for o in owners]
    if wids != ["worker-1", "worker-2", "worker-1"] or owners[0] != t.w1_owner or owners[0] == owners[2]:
        t.fail("expected the lock to go worker-1 -> worker-2 -> restarted worker-1, saw %s" % epochs)
    item = t.snapshot.get("item")
    if not item or not (item["owner"] or "").startswith("worker-1:") or item["owner"] == t.w1_owner:
        t.fail("expected the restarted worker-1 to hold the lock at the end, found %s" % item)
    if not t.ctx.get("dropped"):
        t.notes.append("worker-2's acquiring write was never applied-and-dropped")
    for t_sig, wid in t.ctx["handovers"]:
        log = t.proxy_log(wid)
        acq = [r for r in log if r["kind"] == "ddb" and r["lock_table"] and r["op"] in H.DDB_WRITE_OPS
               and r.get("t_fwd") is not None and r.get("status") == 200 and r["t_recv"] >= t_sig]
        puts = [r for r in log if r["kind"] == "s3" and r["ledger"] and r["op"] == "PUT"
                and r.get("status") == 200 and r["t_recv"] >= t_sig and r.get("t_resp") is not None]
        first_acq = ("%.2f s" % (acq[0]["t_fwd"] - t_sig)) if acq else "never"
        if not puts:
            t.fail("after the holder's SIGTERM, %s never appended (lock write landed: %s)"
                   % (wid, first_acq))
            continue
        gap = puts[0]["t_resp"] - t_sig
        t.notes.append("handover to %s: lock write at +%s, first append at +%.2f s"
                       % (wid, first_acq, gap))
        if gap * 1000 > H.HANDOVER_MS:
            t.fail("deploy stalled the ledger: %s first appended %.1f s after the holder's SIGTERM "
                   "(README: within %d s)" % (wid, gap, H.HANDOVER_MS // 1000))


CHECKS = {"A1": check_a, "A2": check_a, "B1": check_b, "B2": check_b, "C": check_c, "R": check_r}


@pytest.mark.parametrize("case,repeat", CASES, ids=["%s-%d" % c for c in CASES])
def test_trial(case, repeat):
    t = H.Trial(case, repeat)
    t.ctx = {}
    try:
        t.setup()
        if t.warmup():
            RUNNERS[case](t)
    except Exception as err:  # harness-side problem: still audit what happened
        t.fail("harness error during live phase: %r" % err)
    finally:
        try:
            t.finish()
        except Exception as err:
            t.fail("harness error while collecting state: %r" % err)
    if t.ctx.get("observed") and t.snapshot is not None:
        try:
            CHECKS[case](t)
        except Exception as err:
            t.fail("verifier error while auditing: %r" % err)
    print(t.report())
    assert not t.failures, t.report()
