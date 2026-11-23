"""Verifier for chambersm: history and current values under sequential, concurrent and killed
writers. Every expectation is computed from Secrets Manager state read directly from Floci and
from the driver's own record of which writes exited 0 and when."""

import json
import os
import time

import pytest

import harness as h

K1, K2, K4 = "db_password", "stripe_api_key", "session_secret"
SWEEP_KEYS = (K1, K2, K4)
SEQ_KEYS = ("db_password", "api_token", "smtp_password")

RETAINED_WRITES = 15      # README "History retention"

CATEGORIES = ("durability", "coherence", "writers", "format", "cli", "accounting")


# --------------------------------------------------------------------------------- checks

def evaluate(run, keys):
    """Read the service back from Floci and record every violation on run."""
    state = h.State(run.service)
    os.makedirs(run.logdir, exist_ok=True)
    h.dump_trace(run, os.path.join(run.logdir, "trace.jsonl"))
    with open(os.path.join(run.logdir, "state.json"), "w") as fh:
        json.dump({"stages": state.stages, "listed": state.listed,
                   "all_versions": sorted(state.all_ids), "docs": state.docs,
                   "current": state.current}, fh, indent=1)
    if not state.exists:
        run.fail("durability", f"secret {run.service} does not exist")
        return

    rep, problems = h.replay(state, keys)
    for p in problems:
        run.fail("coherence", p)
    with open(os.path.join(run.logdir, "replay.json"), "w") as fh:
        json.dump({k: {str(v): sorted(map(str, vals)) for v, vals in sorted(d.items())}
                   for k, d in rep.items()}, fh, indent=1)

    by_value = {w.value: w for w in run.writes}

    # Where each write's value is visible in history. History only has to reach back
    # RETAINED_WRITES writes: a write must still be there while fewer than that many other writes
    # could have come after it (every write that had not finished before it started).
    found = {}
    for w in run.writes:
        found[w.value] = sorted(v for v, vals in rep[w.key].items() if w.value in vals)
        where = found[w.value]
        later = sum(1 for y in run.writes if y is not w and not (y.end < w.start))
        if w.acked and w.retained_at_start and not where and later < RETAINED_WRITES:
            run.fail("durability", f"{w.describe()} exited 0 and only {later} writes came after "
                                   f"it, but no version Secrets Manager still lists records it, "
                                   f"so history has lost it")
        if len(where) > 1:
            run.fail("coherence", f"{w.describe()} shows up in history as {w.key} versions {where}")

    for key in keys:
        for version, vals in sorted(rep[key].items()):
            if len(vals) > 1:
                run.fail("coherence", f"{key} version {version} has {len(vals)} different values in "
                                      f"listed versions: {sorted(map(str, vals))}")
            for val in vals:
                w = by_value.get(val)
                if w is None or w.key != key:
                    run.fail("coherence", f"{key} version {version} = {val!r}, which was never "
                                          f"written to {key}")

    # Writes that did not overlap must be numbered in the order they happened. A write whose
    # process died counts as having happened, if at all, before it died.
    versioned = [w for w in run.writes if len(found[w.value]) == 1]
    for x in versioned:
        for y in versioned:
            if x is y or x.key != y.key or not (x.end < y.start):
                continue
            vx, vy = found[x.value][0], found[y.value][0]
            if not vx < vy:
                what = "finished" if x.acked else "was killed"
                run.fail("coherence", f"{x.describe()} {what} before {y.describe()} started, but "
                                      f"history numbers them {x.key} v{vx} and v{vy}")

    try:
        cur_doc, cur_meta = h.parse_doc(state.current or "")
    except ValueError as err:
        run.fail("format", f"AWSCURRENT is not a chambersm document ({err})")
        cur_doc, cur_meta = {}, {}

    for key in keys:
        acked = [w for w in run.writes if w.key == key and w.acked]
        if not acked and key not in cur_doc:
            continue
        cur_val = cur_doc.get(key)
        w = by_value.get(cur_val)
        if w is None or w.key != key:
            run.fail("durability", f"current {key} is {cur_val!r}, which was never written to it")
        else:
            for y in acked:
                if w.end < y.start:
                    run.fail("durability", f"current {key} is still {cur_val!r} from {w.describe()}, "
                                           f"although {y.describe()} started after that and exited 0")
        if rep[key]:
            top = max(rep[key])
            vals = rep[key][top]
            if len(vals) == 1:
                (top_val,) = vals
                if cur_val != top_val:
                    run.fail("coherence", f"current {key} is {cur_val!r} but the highest version in "
                                          f"history, v{top}, is {top_val!r}")
            entry = cur_meta.get(key)
            cur_version = entry.get("version") if isinstance(entry, dict) else None
            if cur_version != top:
                run.fail("coherence", f"current metadata gives {key} version {cur_version!r} but "
                                      f"history's highest version is {top}")

    expected_top = {w.key for w in run.writes if w.acked} | {h.METADATA_KEY}
    allowed_top = set(keys) | {h.METADATA_KEY}
    top_level = set(cur_doc)
    if cur_doc and not (expected_top <= top_level <= allowed_top):
        run.fail("format", f"AWSCURRENT top-level fields are {sorted(top_level)}; expected the "
                           f"service's keys {sorted(expected_top - {h.METADATA_KEY})} plus "
                           f"{h.METADATA_KEY}")

    for p in h.account(run, state):
        run.fail("accounting", p)

    # The CLI must report what is stored. History replays the listed versions; it must show
    # every one that "History retention" still covers and may leave out older ones.
    def required(key, val):
        w = by_value.get(val)
        if w is None or w.key != key:
            return True
        return sum(1 for y in run.writes if y is not w and not (y.end < w.start)) < RETAINED_WRITES

    for key in keys:
        if not rep[key] or any(len(v) != 1 for v in rep[key].values()):
            continue
        expected = {(v, next(iter(vals))) for v, vals in rep[key].items()}
        must = {(v, val) for v, val in expected if required(key, val)}
        rc, out, err, timed_out = run.cli(["history", run.service, key])
        if rc != 0:
            run.fail("cli", f"`history {run.service} {key}` exited {rc}: {err}")
            continue
        try:
            got = set()
            for line in out.splitlines():
                if line.strip():
                    event = json.loads(line)
                    got.add((event["version"], event["value"]))
        except (ValueError, KeyError, TypeError) as exc:
            run.fail("cli", f"`history {run.service} {key}` printed unparseable output ({exc}): "
                            f"{out[:300]!r}")
            continue
        if not got <= expected:
            run.fail("cli", f"`history {run.service} {key}` shows {sorted(got - expected)}, which "
                            f"no version Secrets Manager lists holds; the listed versions hold "
                            f"{sorted(expected)}")
        if not must <= got:
            run.fail("cli", f"`history {run.service} {key}` shows {sorted(got)} and leaves out "
                            f"{sorted(must - got)}, which fewer than {RETAINED_WRITES} writes "
                            f"have followed")
    for key in keys:
        if key not in cur_doc:
            continue
        rc, out, err, _ = run.cli(["read", run.service, key])
        if rc != 0 or out.rstrip("\n") != cur_doc[key]:
            run.fail("cli", f"`read {run.service} {key}` gave rc={rc} {out.strip()!r}, "
                            f"current value is {cur_doc[key]!r}: {err}")
    key = keys[0]
    if rep[key] and all(len(v) == 1 for v in rep[key].values()):
        for version, vals in sorted(rep[key].items()):
            (val,) = vals
            rc, out, err, _ = run.cli(["read", run.service, key, "--version", str(version)])
            if rc == 0 and out.rstrip("\n") == val:
                continue
            if rc != 0 and not required(key, val):
                continue          # old enough to have dropped out of history
            run.fail("cli", f"`read {run.service} {key} --version {version}` gave rc={rc} "
                            f"{out.strip()!r}, history holds {val!r}: {err}")


# ------------------------------------------------------------------------------- scenarios

def sequential_scenario():
    run = h.Run(h.service_name("ledger"), os.path.join(h.LOG_DIR, "sequential"))
    k1, k2, k3 = SEQ_KEYS
    plan = [(k1, "p1"), (k1, "p2"), (k2, "q1"), (k2, "q2"), (k1, "p3"), (k3, "r1"), (k2, "q3"),
            (k3, "r2"), (k1, "p4")]
    # Then well past the label quota: one key rotated often, the others now and then.
    plan += [(k1, f"p{i}") if i % 3 else (k2 if i % 2 else k3, f"x{i}") for i in range(5, 22)]
    for i, (key, tag) in enumerate(plan, 1):
        run.sequential_write(f"s{i}", key, h.nonce(tag), "sequential")
    evaluate(run, SEQ_KEYS)
    run.cleanup()
    return run


# A service the previous chambersm has been writing for a while: one key rotated often, then a
# new key. The per-key CHAMBER{version} labels have filled the secret's 20-label quota, and the
# most recent versions hold the lowest-numbered labels.
LEGACY_KEYS = ("db_password", "webhook_secret", "smtp_password")


def legacy_scenario():
    run = h.Run(h.service_name("payments"), os.path.join(h.LOG_DIR, "legacy"))
    k1, k2, k3 = LEGACY_KEYS
    plan = [(k1, h.nonce(f"l{i}")) for i in range(1, 19)]
    plan += [(k2, h.nonce(f"w{i}")) for i in range(1, 4)]
    seeded = run.seed(plan)
    rep, _ = h.replay(h.State(run.service), LEGACY_KEYS)
    for w in seeded:
        w.retained_at_start = any(w.value in vals for vals in rep[w.key].values())
    for i, (key, tag) in enumerate([(k1, "n1"), (k2, "n2"), (k3, "n3"), (k1, "n4"), (k2, "n5"),
                                    (k1, "n6")], 1):
        run.sequential_write(f"n{i}", key, h.nonce(tag), "on a service at the label quota")
    evaluate(run, LEGACY_KEYS)
    run.cleanup()
    return run


# Enough writes before the race that the secret is at its label quota when it starts.
WARMUP = [(K1, "a"), (K2, "b"), (K1, "a"), (K4, "e"), (K1, "a"), (K2, "b"), (K1, "a"), (K2, "b"),
          (K4, "e"), (K1, "a"), (K2, "b"), (K1, "a"), (K4, "e"), (K2, "b"), (K1, "a"), (K2, "b"),
          (K1, "a"), (K2, "b")]


def killed_writer_probe(run, key):
    """Right after a writer was killed, before anyone writes again, `history` and `read` must
    agree: the newest version history shows is the current value."""
    rc, out, err, _ = run.cli(["history", run.service, key])
    if rc != 0:
        run.fail("coherence", f"`history {run.service} {key}` after a killed writer exited {rc}: "
                              f"{err}")
        return
    try:
        shown = {}
        for line in out.splitlines():
            if line.strip():
                event = json.loads(line)
                shown.setdefault(event["version"], set()).add(event["value"])
    except (ValueError, KeyError, TypeError) as exc:
        run.fail("cli", f"`history {run.service} {key}` printed unparseable output ({exc}): "
                        f"{out[:300]!r}")
        return
    rc, cur, err, _ = run.cli(["read", run.service, key])
    if rc != 0 or not shown:
        run.fail("coherence", f"after a killed writer, `read {run.service} {key}` gave rc={rc} "
                              f"and `history` shows {len(shown)} versions: {err}")
        return
    top = max(shown)
    cur = cur.rstrip("\n")
    if shown[top] != {cur}:
        run.fail("coherence", f"after a killed writer and before the next write, `history "
                              f"{run.service} {key}` shows v{top} = {sorted(shown[top])} but "
                              f"`read` gives {cur!r}")


def sweep_run(t):
    """One run of the sweep at index t. Returns (run, whether B, C and E all made fewer than t
    change requests)."""
    run = h.Run(h.service_name(f"billing-t{t}"), os.path.join(h.LOG_DIR, f"sweep-t{t:02d}"))
    for i, (key, tag) in enumerate(WARMUP, 1):
        run.sequential_write(f"s{i}", key, h.nonce(f"{tag}{i}"), "setup")

    # B stalls at its t-th change request; A runs start to finish meanwhile.
    b = run.spawn_write("B", K1, h.nonce("d1"), park_at=t)
    gate = time.monotonic() + h.GATE_TIMEOUT_S
    while time.monotonic() < gate:
        if b.proc.proxy.parked.is_set() or b.proc.proc.poll() is not None:
            break
        time.sleep(0.01)
    b_parked = b.proc.proxy.parked.is_set()
    b_exhausted = False
    if not b_parked:
        if b.proc.proc.poll() is None:
            run.fail("writers", f"{b.describe()} made no further Secrets Manager call and did not "
                                f"finish within {h.GATE_TIMEOUT_S} s")
            run.finish(b, 0.1, "race")
        else:
            run.finish(b, 5, "race")
            b_exhausted = b.acked
    a = run.spawn_write("A", K1, h.nonce("c1"))
    run.finish(a, h.WRITE_DEADLINE_S, "race, while B was stalled" if b_parked else "race")
    if b_parked:
        b.proc.proxy.release.set()
        run.finish(b, h.WRITE_DEADLINE_S, "race, after resuming")
    run.sequential_write("f1", K4, h.nonce("e1"), "after race")
    run.sequential_write("f2", K4, h.nonce("e2"), "after race")

    # C is killed right after its t-th change request was applied; D follows.
    c = run.spawn_write("C", K2, h.nonce("g1"), kill_at=t)
    run.finish(c, h.WRITE_DEADLINE_S, "crash")
    c_exhausted = c.acked
    if c.killed:
        killed_writer_probe(run, K2)
    run.sequential_write("D", K2, h.nonce("h1"), "after a killed writer")
    run.sequential_write("f3", K4, h.nonce("e3"), "after a killed writer")

    # E's t-th change request is applied, but the connection drops before the answer arrives;
    # E's SDK retries it. F follows.
    e = run.spawn_write("E", K1, h.nonce("k1"), drop_at=t)
    run.finish(e, h.WRITE_DEADLINE_S, "connection dropped after a request was applied")
    e_exhausted = e.acked and e.proc.proxy.mutations < t
    run.sequential_write("F", K1, h.nonce("k2"), "after a dropped connection")

    evaluate(run, SWEEP_KEYS)
    run.cleanup()
    return run, b_exhausted and c_exhausted and e_exhausted


# -------------------------------------------------------------------------------- fixtures

_CACHE = {}


def _preflight():
    if not h.floci_alive():
        with open("/logs/verifier/environment", "w") as fh:
            fh.write("floci not reachable\n")
        pytest.fail(f"environment: Floci is not reachable at {h.FLOCI_ENDPOINT}")


def _list_secret_names():
    sm = h.floci_client()
    names, kwargs = set(), {}
    while True:
        resp = sm.list_secrets(**kwargs)
        names.update(s["Name"] for s in resp.get("SecretList", []))
        if not resp.get("NextToken"):
            return names
        kwargs["NextToken"] = resp["NextToken"]


@pytest.fixture(scope="session")
def baseline_names():
    _preflight()
    return _list_secret_names()


@pytest.fixture(scope="session")
def sequential(baseline_names):
    if "sequential" not in _CACHE:
        _CACHE["sequential"] = sequential_scenario()
    return _CACHE["sequential"]


@pytest.fixture(scope="session")
def legacy(baseline_names):
    if "legacy" not in _CACHE:
        _CACHE["legacy"] = legacy_scenario()
    return _CACHE["legacy"]


@pytest.fixture(scope="session")
def sweep(baseline_names):
    if "sweep" not in _CACHE:
        runs, outcome = [], "index limit"
        for t in range(1, h.MAX_INDEX + 1):
            run, exhausted = sweep_run(t)
            runs.append(run)
            if run.violations:
                outcome = "violations"   # evidence enough; later indices only cost time
                break
            if exhausted:
                outcome = "covered"
                break
        _CACHE["sweep"] = (runs, outcome)
    return _CACHE["sweep"]


def _report(runs, category):
    lines = []
    for run in runs:
        for cat, msg in run.violations:
            if cat == category:
                lines.append(f"[{run.service}] {msg}")
    return lines


# ----------------------------------------------------------------------------------- tests

def test_sequential_writes_keep_full_history(sequential):
    problems = [f"[{c}] {m}" for c, m in sequential.violations]
    assert not problems, "\n".join(problems)


def test_service_already_at_label_quota_keeps_working(legacy):
    problems = [f"[{c}] {m}" for c, m in legacy.violations]
    assert not problems, "\n".join(problems)


def test_acknowledged_writes_survive_concurrent_and_killed_writers(sweep):
    runs, _ = sweep
    problems = _report(runs, "durability")
    assert not problems, "\n".join(problems)


def test_history_stays_coherent(sweep):
    runs, _ = sweep
    problems = _report(runs, "coherence")
    assert not problems, "\n".join(problems)


def test_writers_finish_without_waiting_on_others(sweep):
    runs, outcome = sweep
    problems = _report(runs, "writers")
    if outcome == "index limit":
        problems.append(f"writes still issued a change request at index {h.MAX_INDEX}; a single "
                        f"write should not need that many Secrets Manager change calls")
    assert not problems, "\n".join(problems)


def test_secret_format_unchanged(sweep, sequential, legacy):
    runs, _ = sweep
    problems = _report(runs + [sequential, legacy], "format")
    assert not problems, "\n".join(problems)


def test_cli_reports_what_is_stored(sweep):
    runs, _ = sweep
    problems = _report(runs, "cli")
    assert not problems, "\n".join(problems)


def test_all_changes_go_through_the_writer_endpoint(sweep, sequential, legacy, baseline_names):
    runs, _ = sweep
    problems = _report(runs + [sequential, legacy], "accounting")
    ours = {r.service for r in runs + [sequential, legacy]}
    extra = _list_secret_names() - baseline_names - ours
    if extra:
        problems.append(f"secrets other than the services written appeared: {sorted(extra)}")
    assert not problems, "\n".join(problems)
