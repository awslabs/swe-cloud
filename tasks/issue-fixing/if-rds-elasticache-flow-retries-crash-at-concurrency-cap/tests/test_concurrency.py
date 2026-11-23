"""Deployment concurrency under retries, reschedules, dead processes and server restarts.

One workload (harness.run_case) runs four limit-1 deployments at once:
  D1  R1 fails, retries in-process after 8 s, fails again after a 16 s second attempt; R2, R3 queue.
  D2  R4 fails once and retries; 1 s into its second attempt its supervisor gets SIGTERM; R5 queues.
  D3  R6's supervisor and engine are SIGKILLed; once R7 runs, R6 is force-marked Crashed; R8 queues.
  D4  as D3 for R9, and the server process that returns R9's expired lease is SIGKILLed at its
      pause point and restarted; R9 is force-marked Crashed later; R10, R11 queue.
"""

from __future__ import annotations

import harness

TERMINAL = harness.TERMINAL
ADMIT_S = 4.0            # a freed slot is taken by the next queued run within the worker's poll cycle
RESTART_ADMIT_S = 8.0    # after the dead holder is closed out, its slot is free again
RECLAIM_S = 12.0         # a dead holder's slot is back within one lease TTL plus a few scan cycles


def _final(rec, name):
    states = rec.states.get(name) or []
    return states[-1] if states else {}


def _first(rec, name, state_type, after=None):
    for s in rec.states.get(name) or []:
        if s["type"] == state_type and (after is None or s["ts"] > after):
            return s["ts"]
    return None


def _describe(rec, name):
    return " -> ".join(f"{s['name']}@{s['ts'] - rec.t0:.1f}" for s in rec.states.get(name) or [])


def _attempt_intervals(rec, dep):
    out = []
    kills = {"R6": rec.marks.get("d3_kill"), "R9": rec.marks.get("d4_kill")}
    for name, info in rec.runs.items():
        if info["dep"] != dep:
            continue
        states = rec.states.get(name) or []
        for a in rec.attempts.get(name) or []:
            end = a["end"]
            if end is None:
                # The engine was killed mid-body: by the verifier (R6, R9) or by its supervisor
                # right after a reschedule, which the supervisor records.
                candidates = [rec.end]
                if kills.get(name) and kills[name] > a["start"]:
                    candidates.append(kills[name])
                candidates += [e["t"] for e in rec.events if e.get("ev") == "engine_killed"
                               and e.get("run") == info["id"] and e["t"] > a["start"]]
                if len(candidates) == 1:
                    later = [s["ts"] for s in states if s["ts"] > a["start"] and s["type"] != "RUNNING"]
                    candidates += [later[0] + 0.5] if later else []
                end = min(candidates)
            out.append((a["start"], end, name, a["attempt"]))
    return sorted(out)


# ------------------------------------------------------------------------------------------- D1

def test_in_process_retry_runs_to_completion(rec):
    final = _final(rec, "R1")
    assert final.get("type") == "FAILED", f"R1 should end Failed after its retry; history: {_describe(rec, 'R1')}"
    assert not any(s["type"] == "CRASHED" for s in rec.states["R1"]), f"R1 crashed: {_describe(rec, 'R1')}"
    attempts = rec.attempts["R1"]
    assert [a["attempt"] for a in attempts] == [1, 2], f"R1 attempts: {attempts}"
    assert all(a["outcome"] == "fail" for a in attempts), f"R1 attempts did not run their bodies out: {attempts}"
    refused = [e for e in rec.events if e.get("ev") == "renew" and e.get("run") == rec.rid("R1")
               and e.get("status") != 204]
    assert not refused, f"R1's engine had lease renewals refused: {refused}"


def test_retrying_run_keeps_its_slot(rec):
    failed_at = _first(rec, "R1", "FAILED")
    assert failed_at, f"R1 never reached Failed: {_describe(rec, 'R1')}"
    for name in ("R2", "R3"):
        started = _first(rec, name, "RUNNING")
        assert started is None or started > failed_at, (
            f"{name} ran at +{started - rec.t0:.1f}s while R1 (limit 1) was still retrying "
            f"(R1: {_describe(rec, 'R1')})")
    attempts = rec.attempts["R1"]
    if len(attempts) >= 2 and attempts[0]["end"]:
        lo, hi = attempts[0]["end"] + 0.5, attempts[1]["start"] - 0.5
        during = [s["D1"] for t, s in rec.samples if lo <= t <= hi and "D1" in s]
        assert during and all(v == 1 for v in during), (
            f"D1 active_slots during R1's retry delay should stay 1, saw {sorted(set(during))}")


def test_finished_retry_hands_the_slot_on(rec):
    failed_at = _first(rec, "R1", "FAILED")
    assert failed_at, f"R1 never reached Failed: {_describe(rec, 'R1')}"
    r2 = _first(rec, "R2", "RUNNING", after=failed_at)
    assert r2 and r2 - failed_at <= ADMIT_S, (
        f"R2 should start within {ADMIT_S}s of R1's Failed; R2: {_describe(rec, 'R2')}")
    for name in ("R2", "R3"):
        assert _final(rec, name).get("type") == "COMPLETED", f"{name}: {_describe(rec, name)}"


# ------------------------------------------------------------------------------------------- D2

def test_rescheduled_run_gives_its_slot_back(rec):
    assert "d2_error" not in rec.marks, f"{rec.marks.get('d2_error')}; R4: {_describe(rec, 'R4')}"
    resched = rec.marks.get("d2_resched")
    assert resched and resched["type"] == "SCHEDULED", f"R4 after SIGTERM: {_describe(rec, 'R4')}"
    lease = rec.marks.get("L4")
    assert lease, "R4's second attempt ran without a lease"
    probe = rec.marks.get("d2_probe")
    assert probe == 410, (
        f"renewing R4's lease {lease} {harness.RELEASE_PROBE_S}s after its reschedule was recorded "
        f"answered {probe}; the rescheduled run no longer holds a slot")
    nxt = min(filter(None, (_first(rec, n, "RUNNING", after=resched["ts"]) for n in ("R4", "R5"))), default=None)
    assert nxt and nxt - resched["ts"] <= ADMIT_S, (
        f"no D2 run started within {ADMIT_S}s of R4's reschedule; R4: {_describe(rec, 'R4')}; "
        f"R5: {_describe(rec, 'R5')}")
    for name in ("R4", "R5"):
        assert _final(rec, name).get("type") == "COMPLETED", f"{name}: {_describe(rec, name)}"


# ------------------------------------------------------------------------------------------- D3

def test_dead_holder_slot_comes_back_once(rec):
    assert "d3_error" not in rec.marks, rec.marks.get("d3_error")
    killed = rec.marks["d3_kill"]
    r7 = _first(rec, "R7", "RUNNING")
    assert r7 and r7 - killed <= RECLAIM_S, (
        f"R7 should start within {RECLAIM_S}s of R6's process dying; R7: {_describe(rec, 'R7')}")
    status, body = rec.marks.get("d3_crash") or (None, None)
    assert status == 200 and body.get("status") == "ACCEPT", f"forced Crashed for R6: {status} {body}"
    assert _final(rec, "R6").get("type") == "CRASHED", f"R6: {_describe(rec, 'R6')}"
    r7_done = _first(rec, "R7", "COMPLETED")
    r8 = _first(rec, "R8", "RUNNING")
    assert r7_done and r8 and r8 > r7_done, (
        f"R8 must not start before R7 finished (limit 1); R7: {_describe(rec, 'R7')}; R8: {_describe(rec, 'R8')}")
    assert _final(rec, "R8").get("type") == "COMPLETED", f"R8: {_describe(rec, 'R8')}"


# ------------------------------------------------------------------------------------------- D4

def test_slot_survives_a_restart_mid_release(rec):
    assert "d4_error" not in rec.marks, rec.marks.get("d4_error")
    crash_sent = rec.marks["d4_crash_sent"]
    status, body = rec.marks.get("d4_crash") or (None, None)
    assert status == 200 and body.get("status") == "ACCEPT", f"forced Crashed for R9: {status} {body}"
    r10 = _first(rec, "R10", "RUNNING")
    assert r10 and r10 - crash_sent <= RESTART_ADMIT_S, (
        f"R10 should be running at the latest {RESTART_ADMIT_S}s after R9 was closed out; R10: {_describe(rec, 'R10')}; "
        f"D4 active_slots at the end: {rec.final_slots.get('D4')}")
    r10_done = _first(rec, "R10", "COMPLETED")
    r11 = _first(rec, "R11", "RUNNING")
    assert r10_done and r11 and r11 > r10_done, (
        f"R11 must not start before R10 finished (limit 1); R10: {_describe(rec, 'R10')}; R11: {_describe(rec, 'R11')}")
    for name in ("R10", "R11"):
        assert _final(rec, name).get("type") == "COMPLETED", f"{name}: {_describe(rec, name)}"


# ------------------------------------------------------------------------------------- invariants

def test_limit_never_exceeded(rec):
    problems = []
    for dep in ("D1", "D2", "D3", "D4"):
        intervals = _attempt_intervals(rec, dep)
        for (s1, e1, n1, a1), (s2, e2, n2, a2) in zip(intervals, intervals[1:]):
            if s2 < e1:
                problems.append(f"{dep}: {n1} attempt {a1} (+{s1 - rec.t0:.1f}..+{e1 - rec.t0:.1f}) overlaps "
                                f"{n2} attempt {a2} (+{s2 - rec.t0:.1f}..+{e2 - rec.t0:.1f})")
    bad = sorted({(dep, v) for _, s in rec.samples for dep, v in s.items() if v < 0 or v > 1})
    assert not problems and not bad, "; ".join(problems) + (f"; active_slots out of [0, 1]: {bad}" if bad else "")


def test_released_leases_cannot_be_renewed(rec):
    assert rec.renews, "no lease ids were observed"
    wrong = {k: v for k, v in rec.renews.items() if v != 410}
    assert not wrong, f"renewing a lease that was given back must answer 410: {wrong}"
    resurrected = [k for k, v in rec.final_valkey.get("renewed_keys", {}).items() if v]
    assert not resurrected, f"renewing a dead lease left a lease record behind: {resurrected}"


def test_everything_settles(rec):
    assert not [e for e in rec.errors if "did not finish" in e], rec.errors
    unfinished = {n: _describe(rec, n) for n in rec.runs if _final(rec, n).get("type") not in TERMINAL}
    assert not unfinished, f"runs left unfinished: {unfinished}"
    assert all(v == 0 for v in rec.final_slots.values()), f"active_slots after everything finished: {rec.final_slots}"
    assert rec.final_valkey.get("zcard") == 0 and not rec.final_valkey.get("lease_keys"), (
        f"lease records left after everything finished: {rec.final_valkey}")
