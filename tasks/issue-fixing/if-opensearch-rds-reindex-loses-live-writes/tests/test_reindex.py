"""A schema-2 reindex under live storefront traffic: search stays current, and the promoted index equals the catalog."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402

# (rng seed, which page the export API holds, after which page the ops node running bin/reindex is drained)
SEEDS = [(11, 4, 18), (23, 3, 26), (37, 24, 18)]
_engines: dict = {}
_results: dict = {}


def engines() -> harness.Engines:
    if "error" in _engines:
        pytest.fail(f"environment: {_engines['error']}")
    if "ok" not in _engines:
        try:
            _engines["ok"] = harness.Engines.provision()
        except harness.EnvironmentFailure as error:
            _engines["error"] = str(error)
            harness.LOGS.mkdir(parents=True, exist_ok=True)
            (harness.LOGS / "environment").write_text(f"environment failure: {error}\n")
            pytest.fail(f"environment: {error}")
    return _engines["ok"]


def run(seed: int) -> harness.Result:
    if seed not in _results:
        subprocess.run(["pkill", "-f", "search_sync"], check=False)
        held, kill = {s: (h, k) for s, h, k in SEEDS}[seed]
        try:
            _results[seed] = harness.Scenario(engines(), seed, held, kill).run()
        except harness.EnvironmentFailure as error:
            (harness.LOGS / "environment").write_text(f"environment failure: {error}\n")
            pytest.fail(f"environment: {error}")
    result = _results[seed]
    if result.error:
        pytest.fail(f"scenario could not complete: {result.error}")
    return result


def listing(items, limit=8) -> str:
    items = list(items)
    return "; ".join(map(str, items[:limit])) + (f" ... ({len(items)} in all)" if len(items) > limit else "")


SEED_IDS = [s for s, _, _ in SEEDS]


@pytest.mark.parametrize("seed", SEED_IDS)
def test_app_hook_calls_succeed_promptly(seed):
    """Every hook call the server answers is a success, and calls stay inside the app's 2 s.

    An answered error is the hook's own failure. A call that misses 2 s is counted against a small
    allowance: a design that makes hooks wait (on the reindex, a lock, a phase change) misses it on
    hundreds of calls, while this venue's engines occasionally stall a single connect for about 2 s.
    """
    r = run(seed)
    errors = [f"{o.kind} {o.pid}: HTTP {o.hook_status} in {o.hook_ms:.0f} ms" for o in r.ops
              if isinstance(o.hook_status, int) and not 200 <= o.hook_status < 300]
    slow = [f"{o.kind} {o.pid}: {o.hook_status} in {o.hook_ms:.0f} ms" for o in r.ops
            if not isinstance(o.hook_status, int) or o.hook_ms > harness.HOOK_TIMEOUT_S * 1000]
    allowed = int(harness.SLOW_HOOK_ALLOWANCE * len(r.ops))
    assert not r.writer_errors, f"app writes failed: {listing(r.writer_errors)}"
    assert not r.server_died, r.server_died
    assert not errors, f"{len(errors)} of {len(r.ops)} hook calls were answered with an error: {listing(errors)}"
    assert len(slow) <= allowed, (f"{len(slow)} of {len(r.ops)} hook calls got no answer within {harness.HOOK_TIMEOUT_S:.0f} s "
                                  f"(at most {allowed} allowed for venue noise): {listing(slow)}")


@pytest.mark.parametrize("seed", SEED_IDS)
def test_search_reflects_each_change_during_reindex(seed):
    r = run(seed)
    misses = [f"{o.kind} {o.pid} ({o.role}, {'after' if o.after_t0 else 'before'} reindex start): {o.probe}" for o in r.ops if o.probe]
    assert not misses, (f"{len(misses)} of {len(r.ops)} committed changes were not visible through '{harness.ALIAS}' "
                        f"within {harness.LIVE_LAG_S:.0f} s of the hook answering: {listing(misses)}")


@pytest.mark.parametrize("seed", SEED_IDS)
def test_search_current_right_after_swap(seed):
    r = run(seed)
    assert r.swap_seen_at is not None, f"the alias never moved off {harness.OLD_INDEX}"
    assert r.sweep_checked > 0, "no product could be checked after the swap"
    assert not r.sweep_misses, (f"{harness.SWEEP_AFTER_SWAP_S:.0f} s after the alias moved, {len(r.sweep_misses)} of "
                                f"{r.sweep_checked} products changed during the reindex were served stale: {listing(r.sweep_misses)}")


@pytest.mark.parametrize("seed", SEED_IDS)
def test_reindex_completes(seed):
    r = run(seed)
    assert r.interrupted_after_page is not None, (f"bin/reindex exited {r.cli_rc} before it had read page "
                                                  f"#{dict((s, k) for s, _, k in SEEDS)[seed]}; output: {r.cli_tail[-1500:]}")
    assert not r.cli_timed_out, f"bin/reindex still running {harness.REINDEX_DEADLINE_S:.0f} s after the reindex began; output: {r.cli_tail[-1500:]}"
    assert r.cli_rc == 0, (f"bin/reindex, rerun after the first one was paused at page #{r.interrupted_after_page}, "
                           f"exited {r.cli_rc}; output: {r.cli_tail[-1500:]}")


@pytest.mark.parametrize("seed", SEED_IDS)
def test_alias_serves_new_schema_index(seed):
    r = run(seed)
    assert len(r.alias_indices) == 1, f"'{harness.ALIAS}' names {r.alias_indices}, not exactly one index"
    assert r.alias_indices[0] != harness.OLD_INDEX, f"'{harness.ALIAS}' still serves {harness.OLD_INDEX}"
    assert r.new_meta == 2, f"{r.alias_indices[0]} has _meta.search_schema = {r.new_meta!r}"
    assert r.new_created_ms >= (r.t0 - 5) * 1000, f"{r.alias_indices[0]} was not created by this reindex"


@pytest.mark.parametrize("seed", SEED_IDS)
def test_promoted_documents_match_catalog(seed):
    r = run(seed)
    assert len(r.alias_indices) == 1, f"'{harness.ALIAS}' names {r.alias_indices}"
    missing = [f"{pid} ({r.role_of(pid)})" for pid in sorted(r.table) if pid not in r.served]
    wrong = [f"{pid} ({r.role_of(pid)}): catalog {row}, search {r.served[pid]}" for pid, row in sorted(r.table.items())
             if pid in r.served and r.served[pid] != harness.v2(row)]
    assert not missing, f"{len(missing)} catalog products are missing from search: {listing(missing)}"
    assert not wrong, f"{len(wrong)} products are served with data that is not their current catalog row: {listing(wrong)}"


@pytest.mark.parametrize("seed", SEED_IDS)
def test_deleted_products_stay_deleted(seed):
    r = run(seed)
    assert len(r.alias_indices) == 1, f"'{harness.ALIAS}' names {r.alias_indices}"
    back = [f"{pid} ({r.role_of(pid)})" for pid in sorted(r.deleted) if pid in r.served]
    assert not back, f"{len(back)} of {len(r.deleted)} deleted products are in search: {listing(back)}"
    assert r.alias_count == len(r.table), f"search counts {r.alias_count} documents, the catalog has {len(r.table)} products"


@pytest.mark.parametrize("seed", SEED_IDS)
def test_reindex_copies_catalog_once(seed):
    r = run(seed)
    assert len(r.alias_indices) == 1 and r.indexing, f"'{harness.ALIAS}' names {r.alias_indices}"
    writes = int(r.indexing.get("index_total", 0)) + int(r.indexing.get("delete_total", 0))
    budget = r.rows_at_t0 + harness.BUDGET_FACTOR * r.mutations_after_t0
    assert writes <= budget, (f"{writes} index+delete operations reached {r.alias_indices[0]} for {r.rows_at_t0} products and "
                              f"{r.mutations_after_t0} changes during the run (more than a single copy plus a few per change: {budget})")
    one_pass = r.rows_at_t0 + r.inserts_after_t0
    allowed = one_pass + int(harness.REREAD_SLACK * r.rows_at_t0)
    assert r.rows_served <= allowed, (f"the export API served {r.rows_served} rows to bin/reindex and its rerun after the ops "
                                      f"node was drained at page #{r.interrupted_after_page}; one pass over the catalog "
                                      f"is at most {one_pass}: the rerun copied again what had already been copied")
