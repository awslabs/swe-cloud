"""Parameter Store at path "/": two syncs and hand-managed parameters share the root; one payments
run is SIGKILLed right after Floci applied its 2nd create. From T4b on, the first reply to each write
is lost after Floci applied it (the SDK retries). Ops create parameters by hand, untagged and with the
value payments has for them, while payments runs: at T5 one payments is about to create, and, right
after payments deleted them, the key it drops at T5 and one it held when it was removed at T6."""

from __future__ import annotations

import pytest

import harness
import scenarios
from checks import Problems, exact_names, hand_at, has_values, run_basics, unchanged


@pytest.fixture(scope="module")
def tl():
    if not harness.floci_live():
        pytest.fail("environment: Floci is not answering on localhost:4566")
    proxy = harness.FaultProxy()
    return scenarios.run_parameter_store_root(harness.Runner(proxy))


def _names(d):
    return {f"/{k}" for k in d}


def _paths(d):
    return {f"/{k}": v for k, v in d.items()}


def _expected(tl):
    f = tl.facts
    checkout_owned = _paths({k: v for k, v in f["checkout"].items() if f"/{k}" != f["collision_s2"]})
    payments_owned = _paths({k: v for k, v in f["payments"].items() if f"/{k}" != f["collision_s1"]})
    hand_t5 = hand_at(tl, "T5")
    payments_t5 = _paths({k: v for k, v in f["payments_t5"].items()
                          if f"/{k}" != f["collision_s1"] and f"/{k}" not in hand_t5})
    return checkout_owned, payments_owned, payments_t5


def _check(tl, label, *, checkout=None, payments=None, frozen=(), names=None, subset=None):
    step = tl.steps[label]
    p = Problems()
    run_basics(p, step, step.expect_rc)
    hand = hand_at(tl, label)
    unchanged(p, step.state, hand, hand, "hand-managed parameter")
    if names is not None:
        names = set(names) | set(hand)
    for prev_label, owned in frozen:
        unchanged(p, step.state, tl.steps[prev_label].state, owned, "other sync's parameter")
    if checkout is not None:
        has_values(p, step.state, checkout, {"team": "checkout"}, "checkout-root parameter")
    if payments is not None:
        has_values(p, step.state, payments, {"team": "payments"}, "payments-root parameter")
    if names is not None:
        exact_names(p, step.state, names)
    if subset is not None:
        for name in sorted(set(step.state) - subset):
            p.add(f"unexpected entry {name}")
    assert not p, p.verdict(step)


def test_t1_checkout_sync_at_shared_root(tl):
    co, _, _ = _expected(tl)
    _check(tl, "T1", checkout=co, names=set(tl.seeds) | set(co))


def test_t2_payments_run_killed_mid_sync(tl):
    co, pay, _ = _expected(tl)
    step = tl.steps["T2"]
    p = Problems()
    run_basics(p, step, None)
    if not step.run.killed:
        p.add(f"the run did not reach its {scenarios.KILL_AFTER_NTH_CREATE}nd create through AWS_ENDPOINT_URL, "
              f"so the kill never fired (creates={step.run.creates})")
    unchanged(p, step.state, tl.seeds, tl.seeds, "hand-managed parameter")
    unchanged(p, step.state, tl.steps["T1"].state, co, "checkout-root parameter")
    for name in sorted(set(step.state) - set(tl.seeds) - set(co) - set(pay)):
        p.add(f"unexpected entry {name}")
    assert not p, p.verdict(step)


def test_t3_payments_converges_after_kill(tl):
    co, pay, _ = _expected(tl)
    _check(tl, "T3", checkout=co, payments=pay, frozen=[("T2", co)], names=set(tl.seeds) | set(co) | set(pay))


def test_t4_payments_repeat_sync_keeps_everything(tl):
    co, pay, _ = _expected(tl)
    _check(tl, "T4", checkout=co, payments=pay, frozen=[("T3", co)], names=set(tl.seeds) | set(co) | set(pay))


def test_t4b_checkout_resync_leaves_payments_alone(tl):
    co, pay, _ = _expected(tl)
    _check(tl, "T4b", checkout=co, payments=pay, frozen=[("T4", pay)], names=set(tl.seeds) | set(co) | set(pay))


def test_t5_dropped_key_deleted_changed_key_updated_new_keys_created(tl):
    co, _, pay5 = _expected(tl)
    _check(tl, "T5", checkout=co, payments=pay5, frozen=[("T4b", co)], names=set(co) | set(pay5))


def test_t6_remove_payments_deletes_everything_it_created(tl):
    co, _, _ = _expected(tl)
    _check(tl, "T6", checkout=co, frozen=[("T5", co)], names=set(co))


def test_t7_remove_checkout_leaves_only_hand_managed(tl):
    _check(tl, "T7", names=set())
