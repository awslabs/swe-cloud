import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import scenario  # noqa: E402

THROTTLE_TTL_MS = scenario.THROTTLE_TTL_MS


@pytest.fixture(scope="session")
def obs():
    return scenario.run()


def _brief(res):
    if not res:
        return res
    return {k: res.get(k) for k in ("rc", "job_id", "deduplicated", "stderr")}


def created(res):
    return bool(res) and res.get("rc") == 0 and res.get("deduplicated") is False and bool(res.get("job_id"))


def deduped_against(res, owner):
    return bool(res) and res.get("rc") == 0 and res.get("deduplicated") is True and res.get("job_id") == owner


def _errors(obs, *names):
    return {n: obs["errors"][n] for n in names if n in obs["errors"]}


def _jobs_for(obs, dedup_id):
    return {jid for jid, j in obs.get("jobs", {}).items() if j.get("deid") == dedup_id}


def test_schedule_ran(obs):
    assert not obs["errors"], obs["errors"]


# ---- no worker running --------------------------------------------------------------------

def test_duplicate_of_waiting_job_is_deduplicated(obs):
    p0 = obs.get("p0")
    assert p0, _errors(obs, "phase0")
    assert created(p0["first"]), _brief(p0["first"])
    assert deduped_against(p0["dup"], p0["first"]["job_id"]), _brief(p0["dup"])


def test_add_after_owner_record_deleted_creates_job(obs):
    p0 = obs.get("p0")
    assert p0 and "readd" in p0, _errors(obs, "phase0")
    readd = p0["readd"]
    assert created(readd), ("job record of the owner was deleted, no worker running; the next add "
                            "must create a job", _brief(readd))
    assert readd["job_id"] != p0["first"]["job_id"]
    assert p0["de_after"]["value"] == readd["job_id"], p0["de_after"]
    assert p0["readd_hash"].get("deid") == obs["ids"]["ben"], p0["readd_hash"]
    assert p0["readd_hash"].get("state") == "wait", p0["readd_hash"]
    assert not p0["procs_after"], ("dq add left processes running", p0["procs_after"])


def test_stale_key_reclaimed_by_throttled_add(obs):
    p0 = obs.get("p0")
    assert p0 and "t_readd" in p0, _errors(obs, "phase0")
    readd = p0["t_readd"]
    assert created(readd), _brief(readd)
    de = p0["t_de_after"]
    assert de["value"] == readd["job_id"], de
    assert 0 < de["pttl"] <= THROTTLE_TTL_MS, de
    assert not p0["procs_after_t"], p0["procs_after_t"]


def test_add_after_owner_left_the_wait_list_runs(obs):
    p0 = obs.get("p0")
    assert p0 and "g_readd" in p0, _errors(obs, "phase0")
    jg = p0["g_first"]["job_id"]
    assert created(p0["g_first"]), _brief(p0["g_first"])
    assert deduped_against(p0["g_dup"], jg), _brief(p0["g_dup"])
    assert p0["g_dropped"] == 1, p0["g_dropped"]
    readd = p0["g_readd"]
    assert readd["rc"] == 0 and readd["job_id"], _brief(readd)
    assert not p0["g_procs"], ("dq add left processes running", p0["g_procs"])
    pool = obs.get("pool") or {}
    assert pool.get("g_readd_end"), ("the owner's id was taken out of the wait list (its record kept); "
                                     "the job the next add answered with never ran", _brief(readd),
                                     _errors(obs, "pool"))


def test_add_after_owner_left_the_active_list_and_its_worker_died_runs(obs):
    pool = obs.get("pool") or {}
    assert "f_readd" in pool, _errors(obs, "pool")
    assert pool["f_dropped"] == 1, pool["f_dropped"]
    assert pool.get("f_probes"), "no duplicate probes ran"
    assert pool.get("f_lock_gone"), "the killed holder's lock never went away"
    readd = pool["f_readd"]
    assert readd["rc"] == 0 and readd["job_id"], _brief(readd)
    assert pool.get("f_readd_end"), ("the owner's id was taken out of the active list and its worker "
                                     "was killed; the job the next add answered with never ran",
                                     _brief(readd))


def test_concurrent_adds_on_stale_id_create_one_job(obs):
    races = obs.get("race")
    assert races and len(races) == scenario.RACE_IDS, _errors(obs, "race")
    for race in races:
        res = race["results"]
        winners = [r for r in res if created(r)]
        assert len(winners) == 1, (race["id"], [_brief(r) for r in res])
        winner = winners[0]["job_id"]
        losers = [r for r in res if r is not winners[0]]
        assert all(deduped_against(r, winner) for r in losers), (race["id"], winner,
                                                                 [_brief(r) for r in losers])
        jobs = _jobs_for(obs, race["id"]) - {race["first"]["job_id"]}
        assert jobs == {winner}, (race["id"], jobs, winner)
    assert not obs.get("procs_after_race"), obs.get("procs_after_race")


# ---- with workers -------------------------------------------------------------------------

def test_readded_jobs_run(obs):
    pool = obs.get("pool")
    assert pool, _errors(obs, "pool")
    assert pool.get("readd_end"), ("re-added job never finished", _errors(obs, "phase0", "pool"))
    assert pool.get("t_readd_end"), ("re-added job never finished", _errors(obs, "phase0", "pool"))


def test_throttle_window_outlives_its_job(obs):
    pool = obs.get("pool") or {}
    assert "w_de" in pool, _errors(obs, "pool")
    j4 = pool["w_first"]["job_id"]
    assert created(pool["w_first"]), _brief(pool["w_first"])
    assert pool["w_end"] and pool["w_completed"], "throttled job did not run to completion"
    for key in ("w_after_finish", "w_after_delete", "w_after_delete_ttl"):
        assert deduped_against(pool[key], j4), (key, _brief(pool[key]))
    de = pool["w_de"]
    assert de["value"] == j4 and 0 < de["pttl"] <= THROTTLE_TTL_MS, de


def test_owner_keeps_its_id_through_crashes_and_backoff(obs):
    pool = obs.get("pool") or {}
    assert "a_probes" in pool and "c_probes" in pool, _errors(obs, "pool")
    for key in ("a_probes", "c_probes"):
        probes = pool[key]
        assert probes, (key, "no duplicate probes ran", _errors(obs, "pool"))
        bad = [dict(_brief(p), expected=p["expected_owner"], owner_state=p["owner_state_after"], at=p["t0"])
               for p in probes if not deduped_against(p, p["expected_owner"])]
        assert not bad, (key, "a duplicate add was not deduplicated while its owner was in flight", bad)
    assert len(pool["a_probes"]) >= 5, len(pool["a_probes"])


def test_no_second_job_while_owner_in_flight(obs):
    pool = obs.get("pool") or {}
    assert "jobs" in obs and "a_first" in pool and "c_first" in pool, _errors(obs, "pool", "collect")
    for owner_key, readd_key, tag in (("a_first", "a_readd", "ana"), ("c_first", "c_readd", "cai")):
        allowed = {pool[owner_key]["job_id"]}
        if created(pool.get(readd_key)):
            allowed.add(pool[readd_key]["job_id"])
        extra = _jobs_for(obs, obs["ids"][tag]) - allowed
        assert not extra, (tag, "jobs created for an id whose owner was in flight", extra)
    p0 = obs.get("p0") or {}
    if created(p0.get("readd")):
        extra = _jobs_for(obs, obs["ids"]["ben"]) - {p0["readd"]["job_id"]}
        assert not extra, extra


def test_runs_never_overlap(obs):
    assert "runs" in obs, _errors(obs, "collect")
    runs, kills = obs["runs"], obs["kills"]
    deid_of = obs.get("job_deid", {})
    end_ts = obs["end_ts"]
    intervals = {}
    for i, e in enumerate(runs):
        if e["ev"] != "start":
            continue
        close = None
        for later in runs[i + 1:]:
            if later["job"] == e["job"] and later["pid"] == e["pid"] and later["ev"] in ("end", "error"):
                close = later["ts"]
                break
        if close is None:
            killed = [k["ts"] for k in kills if k["pid"] == e["pid"] and k["ts"] >= e["ts"]]
            close = min(killed) if killed else end_ts
        deid = e["deid"] or deid_of.get(e["job"]) or f"job:{e['job']}"
        intervals.setdefault(deid, []).append((e["ts"], close, e["job"], e["pid"]))
    for deid, ivs in intervals.items():
        ivs.sort()
        for prev, nxt in zip(ivs, ivs[1:]):
            assert nxt[0] >= prev[1], (deid, "two runs for one dedup id overlapped", prev, nxt)


def test_crashed_owners_come_back_through_the_stall_checker(obs):
    pool = obs.get("pool") or {}
    assert "a_first" in pool, _errors(obs, "pool")
    j1, j3 = pool["a_first"]["job_id"], pool["c_first"]["job_id"]
    assert pool.get("a_restart"), ("owner not restarted after its worker was killed", _errors(obs, "pool"))
    assert pool.get("c_restart"), ("owner not restarted after its worker was killed", _errors(obs, "pool"))
    assert pool.get("c_failed"), ("owner killed past max-stalled-count did not fail", _errors(obs, "pool"))
    c_starts = [e for e in obs.get("runs", []) if e["ev"] == "start" and e["job"] == j3]
    assert len(c_starts) == 2, ("owner over the stall limit must fail, not run again", c_starts)
    assert pool.get("a_error") and pool.get("a_retry_start"), ("failed attempt was not retried",
                                                               _errors(obs, "pool"))
    assert pool.get("a_final_state") == "completed", pool.get("a_final_state")
    assert pool.get("c_final_state") == "failed", pool.get("c_final_state")
    assert j1 != j3


def test_add_after_stall_limit_failure_creates_job(obs):
    pool = obs.get("pool") or {}
    assert pool.get("c_failed") and "c_readd" in pool, _errors(obs, "pool")
    readd = pool["c_readd"]
    assert created(readd), ("owner failed; the next add must create a job", _brief(readd))
    assert pool["c_de_after"]["value"] in (None, readd["job_id"]), pool["c_de_after"]
    assert pool.get("c_readd_end"), "re-added job never finished"


def test_add_after_completion_creates_job(obs):
    pool = obs.get("pool") or {}
    assert pool.get("a_completed") and "a_readd" in pool, _errors(obs, "pool")
    readd = pool["a_readd"]
    assert created(readd), ("owner completed; the next add must create a job", _brief(readd))
    assert pool["a_de_after"]["value"] in (None, readd["job_id"]), pool["a_de_after"]
    assert pool.get("a_readd_end"), "re-added job never finished"


# ---- keep-last ----------------------------------------------------------------------------

def _starts_of(obs, dedup_id):
    return sorted((e for e in obs.get("runs", []) if e["ev"] == "start" and e["deid"] == dedup_id),
                  key=lambda e: e["ts"])


def _rev(e):
    try:
        return int(e.get("rev"))
    except (TypeError, ValueError):
        return None


def test_keep_last_data_runs_after_stall_limit_failure(obs):
    pool = obs.get("pool") or {}
    assert "k_first" in pool, _errors(obs, "pool")
    jk = pool["k_first"]["job_id"]
    assert created(pool["k_first"]), _brief(pool["k_first"])
    assert pool.get("k_restart"), ("keep-last owner not restarted after its worker was killed",
                                   _errors(obs, "pool"))
    probes = pool.get("k_probes") or []
    assert probes, "no keep-last adds ran while the owner was running"
    bad = [dict(_brief(r), rev=r["rev"]) for r in probes if not deduped_against(r, jk)]
    assert not bad, ("a keep-last add while its owner was running was not deduplicated against it", bad)
    assert pool.get("k_failed"), ("keep-last owner killed past max-stalled-count did not fail",
                                  _errors(obs, "pool"))
    k_starts = [e for e in obs.get("runs", []) if e["ev"] == "start" and e["job"] == jk]
    assert len(k_starts) == 2, ("owner over the stall limit must fail, not run again", k_starts)
    nxt = pool.get("k_next_start")
    assert nxt, ("the owner failed (stall limit) while the latest keep-last add's data was kept; "
                 "that data never ran", {"last_rev": pool.get("k_last_rev")})
    assert nxt["job"] != jk and nxt["ts"] >= pool["kill2_k"], nxt
    assert pool.get("k_next_end"), ("the job with the kept data never finished", nxt)


def test_keep_last_reclaimed_id_never_runs_replaced_data(obs):
    pool = obs.get("pool") or {}
    assert "l_newer" in pool, _errors(obs, "pool")
    jl = pool["l_first"]["job_id"]
    assert created(pool["l_first"]), _brief(pool["l_first"])
    assert deduped_against(pool["l_kept"], jl), _brief(pool["l_kept"])
    assert pool.get("l_lock_gone"), "the killed holder's lock never went away"
    assert pool["l_newer"]["rc"] == 0 and pool["l_newer"]["job_id"], _brief(pool["l_newer"])
    newer = pool.get("l_newer_start")
    assert newer, ("owner record deleted and its worker killed; the newer keep-last add's data "
                   "never ran", _brief(pool["l_newer"]))
    assert pool.get("l_newer_end"), ("the job with the newer data never finished", newer)
    later = [e for e in _starts_of(obs, obs["ids"]["lou"])
             if e["ts"] > newer["ts"] and (_rev(e) is None or _rev(e) < 2)]
    assert not later, ("data older than a run that already happened ran after it", newer, later)


def test_keep_last_owner_taken_out_of_active_list_still_hands_over(obs):
    pool = obs.get("pool") or {}
    assert "t_probes" in pool, _errors(obs, "pool")
    jt = pool["t_first"]["job_id"]
    assert created(pool["t_first"]), _brief(pool["t_first"])
    assert pool["t_dropped"] == 1, pool["t_dropped"]
    bad = [dict(_brief(r), rev=r["rev"]) for r in pool["t_probes"] if not deduped_against(r, jt)]
    assert not bad, ("a keep-last add while its owner was running was not deduplicated against it", bad)
    assert pool.get("t_end"), "the owner's handler never finished"
    nxt = pool.get("t_next_start")
    assert nxt, ("the owner's id had been taken out of the active list while it ran; it then "
                 "completed, and the latest keep-last add's data never ran",
                 {"last_rev": scenario.KIT_ADDS})
    assert nxt["ts"] >= pool["t_end"]["ts"], (nxt, pool["t_end"])
    assert pool.get("t_next_end"), ("the job with the kept data never finished", nxt)


def test_keep_last_never_runs_older_data_after_newer(obs):
    assert "runs" in obs, _errors(obs, "collect")
    for tag in ("kim", "kit", "lou"):
        seen = None
        for e in _starts_of(obs, obs["ids"][tag]):
            rev = _rev(e)
            assert rev is not None, (tag, "a run without the add's data", e)
            assert seen is None or rev >= seen, (tag, "a run with older data than one already run", e)
            seen = rev if seen is None else max(seen, rev)
