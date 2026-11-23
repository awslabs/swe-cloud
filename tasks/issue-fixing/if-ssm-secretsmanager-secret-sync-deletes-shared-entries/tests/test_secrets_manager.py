"""Secrets Manager, account-wide: a one-to-one sync killed right after its 2nd CreateSecret, and a
sync switched from one-to-one to a many-to-one bundle, beside other teams' secrets. Between runs,
secrets get deleted in the console (scheduled with a recovery window): other teams' own, and synced
ones by mistake. From T5 on, the first reply to each write is lost after Floci applied it (the SDK
retries), and teams re-create by hand, right after a sync deleted it, a secret the sync dropped
(T5: a per-key reports secret after the switch; T7: an app-core secret when app-core is removed)."""

from __future__ import annotations

import json

import pytest

import harness
import scenarios
from checks import Problems, exact_names, hand_at, has_values, run_basics, unchanged


@pytest.fixture(scope="module")
def tl():
    if not harness.floci_live():
        pytest.fail("environment: Floci is not answering on localhost:4566")
    proxy = harness.FaultProxy()
    return scenarios.run_secrets_manager(harness.Runner(proxy))


def _app(tl, source):
    return {f"INFISICAL_{k}": v for k, v in source.items() if f"INFISICAL_{k}" not in tl.facts["collisions"]}


def _reports(tl):
    return {f"reports/{k}": v for k, v in tl.facts["reports"].items()}


def _check(tl, label, *, app=None, reports=None, frozen=(), names=None, subset=None, extra=None):
    step = tl.steps[label]
    p = Problems()
    run_basics(p, step, step.expect_rc)
    hand = hand_at(tl, label)
    unchanged(p, step.state, hand, hand, "hand-managed secret")
    if names is not None:
        names = set(names) | set(hand)
    for prev_label, owned in frozen:
        unchanged(p, step.state, tl.steps[prev_label].state, owned, "other sync's secret")
    if app is not None:
        has_values(p, step.state, app, {"team": "core"}, "app-core secret")
    if reports is not None:
        has_values(p, step.state, reports, {"team": "data"}, "reports secret")
    if extra:
        extra(p, step)
    if names is not None:
        exact_names(p, step.state, names)
    if subset is not None:
        for name in sorted(set(step.state) - subset):
            p.add(f"unexpected entry {name}")
    assert not p, p.verdict(step)


def _bundle_ok(tl):
    bundle = tl.facts["bundle"]

    def check(p, step):
        rec = step.state.get(bundle)
        if rec is None or rec.get("deleted"):
            p.add(f"bundle secret {bundle} is missing")
            return
        try:
            value = json.loads(rec.get("value") or "")
        except ValueError:
            p.add(f"bundle secret {bundle} is not JSON: {rec.get('value')!r}")
            return
        if value != tl.facts["reports"]:
            p.add(f"bundle secret {bundle} holds {value}, expected {tl.facts['reports']}")
        if rec.get("tags", {}).get("team") != "data":
            p.add(f"bundle secret {bundle} lacks configured tag team=data (tags={rec.get('tags')})")
    return check


def test_t1_reports_one_to_one(tl):
    rep = _reports(tl)
    _check(tl, "T1", reports=rep, names=set(tl.seeds) | set(rep))


def test_t2_app_run_killed_mid_sync(tl):
    rep, app = _reports(tl), _app(tl, tl.facts["app"])
    step = tl.steps["T2"]
    p = Problems()
    run_basics(p, step, None)
    if not step.run.killed:
        p.add(f"the run did not reach its {scenarios.KILL_AFTER_NTH_CREATE}nd create through AWS_ENDPOINT_URL, "
              f"so the kill never fired (creates={step.run.creates})")
    unchanged(p, step.state, tl.seeds, tl.seeds, "hand-managed secret")
    unchanged(p, step.state, tl.steps["T1"].state, rep, "reports secret")
    for name in sorted(set(step.state) - set(tl.seeds) - set(rep) - set(app)):
        p.add(f"unexpected entry {name}")
    assert not p, p.verdict(step)


def test_t3_app_converges_after_kill(tl):
    rep, app = _reports(tl), _app(tl, tl.facts["app"])
    _check(tl, "T3", app=app, reports=rep, frozen=[("T2", rep)], names=set(tl.seeds) | set(rep) | set(app))


def test_t4_app_repeat_sync_keeps_everything(tl):
    rep, app = _reports(tl), _app(tl, tl.facts["app"])
    _check(tl, "T4", app=app, reports=rep, frozen=[("T3", rep)], names=set(tl.seeds) | set(rep) | set(app))


def test_t5_reports_switch_to_bundle(tl):
    app = _app(tl, tl.facts["app"])
    _check(tl, "T5", app=app, frozen=[("T4", app)], extra=_bundle_ok(tl),
           names=set(tl.seeds) | set(app) | {tl.facts["bundle"]})


def test_t6_app_dropped_key_deleted_and_changed_key_updated(tl):
    app6 = _app(tl, tl.facts["app_t6"])
    bundle = tl.facts["bundle"]
    _check(tl, "T6", app=app6, frozen=[("T5", {bundle})], names=set(tl.seeds) | set(app6) | {bundle})


def test_t7_remove_app(tl):
    bundle = tl.facts["bundle"]
    _check(tl, "T7", frozen=[("T6", {bundle})], names=set(tl.seeds) | {bundle})


def test_t8_remove_reports_leaves_only_hand_managed(tl):
    _check(tl, "T8", names=set(tl.seeds))


def test_t9_removed_name_can_be_synced_again(tl):
    app9 = _app(tl, tl.facts["app_t9"])
    _check(tl, "T9", app=app9, names=set(tl.seeds) | set(app9))
    _check(tl, "T9r", names=set(tl.seeds))
