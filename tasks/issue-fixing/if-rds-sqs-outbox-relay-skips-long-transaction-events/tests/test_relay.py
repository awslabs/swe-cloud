"""Outcome checks on one scenario run (see scenario.py). Each test is independent; none fails fast."""
from __future__ import annotations

import json
import os
import sys
from collections import Counter

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scenario as S  # noqa: E402


@pytest.fixture(scope="session")
def run():
    r = S.get()
    if r.get("environment_error"):
        pytest.fail(f"environment: {r['environment_error']}")
    if not r.get("relay_ready"):
        pytest.fail(f"relay never printed READY; supervisor log: {r.get('supervisor')}")
    return r


def _db(run):
    return {m: (p, d) for p, m, d in run["db_rows"]}


def _first_occurrences(run):
    db = _db(run)
    seen, order, unknown = set(), [], []
    for _t, mid, _pos, _body, _group in run["received"]:
        if mid in seen:
            continue
        seen.add(mid)
        if mid in db:
            order.append(db[mid][0])
        else:
            unknown.append(mid)
    return order, unknown


def test_scenario_was_valid(run):
    """The harness itself behaved (writers committed, slow writer still live at the mid check)."""
    f = run["facts"]
    assert not run["writer_errors"], run["writer_errors"][:3]
    assert f.get("r2_dropped") is True, f
    assert f.get("p_s_committed") and f.get("p_b_committed") and f.get("p_b2_committed"), f
    assert f["p_b"] < f["p_a"] and f["xa"] < f["xb"], f
    assert f["p_b2"] < f["p_a2"] and f["xa2"] < f["xb2"], f
    assert run["phase2"]["slow_xid_in_xip"] is True and run["phase2"]["p_s_row_present"] is False, run["phase2"]
    assert run["bg_rows"] >= S.BG_MIN_ROWS, run["bg_rows"]
    assert run["queue_attributes"] == {"FifoQueue": "true", "ContentBasedDeduplication": "false", "DelaySeconds": "0"}, \
        run["queue_attributes"]
    assert run["replication_slots"] == 0


def test_delivery_resumes_after_rolled_back_and_dropped_writes(run):
    """20 s after the rollback at 8 s (with three SIGKILL restarts in between), the relay is past
    both dead positions and everything committed by 23 s has been delivered."""
    p1, f = run["phase1"], run["facts"]
    assert p1["checkpoint"] >= f["r2"] > f["r1"], (
        f"checkpoint {p1['checkpoint']} at ~{S.PHASE1_AT + S.PHASE1_GRACE}s; rolled-back position {f['r1']}, "
        f"dropped-insert position {f['r2']}")
    assert not p1["missing"], f"{len(p1['missing'])} of {p1['due']} rows committed before 23 s not delivered " \
                              f"by {S.PHASE1_AT + S.PHASE1_GRACE}s, e.g. {p1['missing'][:10]}"


def test_open_transaction_is_waited_for(run):
    """While the reconciliation transaction is open (xid in progress, row not visible), the checkpoint
    stays below its position, and everything below it has been delivered."""
    p2 = run["phase2"]
    assert p2["checkpoint"] < p2["p_s"], f"checkpoint {p2['checkpoint']} passed open position {p2['p_s']} at 60 s"
    assert p2["checkpoint_at_74"] < p2["p_s"], f"checkpoint {p2['checkpoint_at_74']} passed open position {p2['p_s']} at 74 s"
    assert not p2["missing_below_p_s"], f"rows below {p2['p_s']} not delivered by 61 s: {p2['missing_below_p_s'][:10]}"
    q = run["pair2"]
    assert q["checkpoint"] < q["p_b2"], \
        f"checkpoint {q['checkpoint']} passed open position {q['p_b2']} at {S.PAIR2_CHECK_AT:.0f} s"


def test_every_committed_event_delivered(run):
    db = _db(run)
    got = {r[1] for r in run["received"]}
    missing = sorted(db[m][0] for m in db if m not in got)
    f = run["facts"]
    named = {k: f[k] for k in ("p_s", "p_b", "p_a", "p_b2", "p_a2") if f.get(k) in missing}
    assert not missing, f"{len(missing)} committed rows never delivered, e.g. {missing[:15]}; scheduled ones: {named}"


def test_nothing_delivered_that_was_not_committed_or_was_history(run):
    _order, unknown = _first_occurrences(run)
    hist = [m for m in unknown if m.startswith("hist-")]
    assert not hist, f"{len(hist)} history rows at or below the starting checkpoint re-sent, e.g. {hist[:5]}"
    assert not unknown, f"{len(unknown)} messages that match no committed outbox row, e.g. {unknown[:5]}"


def test_position_order(run):
    order, _unknown = _first_occurrences(run)
    bad = [(a, b) for a, b in zip(order, order[1:]) if b <= a]
    f = run["facts"]
    assert not bad, f"{len(bad)} out-of-order deliveries (earlier, later), e.g. {bad[:10]}; " \
                    f"p_s={f.get('p_s')} p_b={f.get('p_b')} p_a={f.get('p_a')} p_b2={f.get('p_b2')} p_a2={f.get('p_a2')}"


def test_single_group_and_bounded_repeats(run):
    groups = Counter(r[4] for r in run["received"])
    assert set(groups) <= {"outbox"}, groups
    copies = Counter(r[1] for r in run["received"])
    over = {m: c for m, c in copies.items() if c > S.MAX_COPIES}
    assert not over, f"messages delivered more than {S.MAX_COPIES} times: {dict(list(over.items())[:5])}"


def test_message_format(run):
    db = _db(run)
    bad = []
    for _t, mid, pos, body, _g in run["received"]:
        if mid not in db:
            continue
        p, data = db[mid]
        try:
            ok = json.loads(body) == json.loads(data) and pos is not None and int(pos) == p
        except (TypeError, ValueError):
            ok = False
        if not ok:
            bad.append((mid, pos, body))
    assert not bad, f"{len(bad)} messages whose body/position attribute does not match the row, e.g. {bad[:3]}"


def test_checkpoint_caught_up(run):
    assert run["checkpoint_final"] == run["head"], (
        f"checkpoint {run['checkpoint_final']} vs max(outbox.position) {run['head']} "
        f"{S.FINAL_AT - S.PAIR2_TIMES[-1] + S.FINAL_CATCHUP:.0f}s+ after the last commit")
