"""Pushes of the digest being deleted, arriving at the other replica while the delete runs."""

import os
import signal
import threading
import time

import pytest

from faultproxy import MUTATING
from harness import TAG_MUTATION, check_end_state, wait_fired_or_done

PARK_LIMIT = 65
STALE_SWEEP_BUDGET = 240


def _push_parked_at_tag_write(cluster, repo, m, d, tag="late", park_limit=PARK_LIMIT):
    """Push tag -> d on replica b, parked just before its write of tags/<tag>/current/link,
    while the janitor deletes d through replica a. The push is released when the janitor is
    done with d, or after park_limit seconds."""
    rule = cluster.rule("b", "hold", ["PUT"], rf"/_manifests/tags/{tag}/current/link$", nth=1)
    push = cluster.b.async_(cluster.b.push, repo, tag, m, timeout=180)
    problems = []
    if not wait_fired_or_done(rule, push, 15):
        cluster.proxy.remove(rule)
        r = push.result(60)
        cluster.note(f"push of {tag} finished without writing its current/link: {r.status}")
        if r.status == 201:
            problems.append(f"push of {tag} answered 201 without writing "
                            f"tags/{tag}/current/link through its S3 endpoint")
        attempts = cluster.janitor_delete(cluster.a, repo, d)
        return attempts, [(tag, r)], problems

    cluster.strict_202 = True
    released = threading.Event()

    def release():
        if not released.is_set():
            released.set()
            cluster.strict_202 = False
            cluster.note(f"releasing parked push of {tag}")
            cluster.proxy.release_all(rule)
            cluster.proxy.remove(rule)

    timer = threading.Timer(park_limit, release)
    timer.start()
    try:
        attempts = cluster.janitor_delete(cluster.a, repo, d)
    finally:
        timer.cancel()
        release()
    r = push.result(120)
    cluster.note(f"push of {tag} -> {r.status}")
    return attempts, [(tag, r)], problems


@pytest.mark.parametrize("run", range(3))
def test_push_during_delete(cluster, run):
    repo, (m, d), (mx, x), seeded = cluster.seed_case("race", 12)
    attempts, pushes, problems = _push_parked_at_tag_write(cluster, repo, m, d)
    problems += check_end_state(cluster, repo, d, seeded, "other", x, pushes=pushes,
                                attempts=attempts)
    problems += cluster.problems_202
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("run", range(2))
def test_push_during_retry_after_crash(cluster, run):
    repo, (m, d), (mx, x), seeded = cluster.seed_case("crashrace", 40)
    cluster.strict_202 = True
    rule = cluster.rule("a", "hold", MUTATING, TAG_MUTATION, nth=1)
    first = cluster.a.async_(cluster.a.delete, repo, d)
    if wait_fired_or_done(rule, first, 15):
        cluster.a.kill()
        cluster.proxy.drop_all(rule)
    cluster.proxy.remove(rule)
    first_result = first.result(60)
    cluster.a.start()
    cluster.strict_202 = False
    problems = []
    if first_result.status == 202:
        problems += cluster.problems_202
        attempts, pushes = [first_result], []
    else:
        attempts, pushes, problems = _push_parked_at_tag_write(cluster, repo, m, d)
        attempts = [first_result] + attempts
    problems += check_end_state(cluster, repo, d, seeded, "other", x, pushes=pushes,
                                attempts=attempts)
    problems += cluster.problems_202
    assert not problems, "\n".join(problems)


def _sweep_case(cluster, label, k, route):
    repo, (m, d), (mx, x), seeded = cluster.seed_case(f"{label}{k}", 3, extra=("moved",))
    late = f"late{k}"
    rule = cluster.rule(route, "hold", None, None, nth=k)
    pushes, moved = [], None
    if route == "a":
        delete = cluster.a.async_(cluster.a.delete, repo, d)
        fired = wait_fired_or_done(rule, delete, 10)
        if fired:
            p_moved = cluster.b.async_(cluster.b.push, repo, "moved", mx, timeout=120)
            p_late = cluster.b.async_(cluster.b.push, repo, late, m, timeout=120)
            for f in (p_moved, p_late):
                try:
                    f.result(10)
                except Exception:
                    pass
        cluster.proxy.release_all(rule)
        cluster.proxy.remove(rule)
        attempts = [delete.result(120)]
        if fired:
            r_moved, r_late = p_moved.result(120), p_late.result(120)
            pushes = [(late, r_late)]
            if r_moved.status == 201:
                moved = ("moved", x)
        if attempts[0].status not in (202, 404):
            attempts += cluster.janitor_delete(cluster.a, repo, d)
    else:
        push = cluster.b.async_(cluster.b.push, repo, late, m, timeout=120)
        fired = wait_fired_or_done(rule, push, 10)
        timer = threading.Timer(15, lambda: (cluster.proxy.release_all(rule),
                                             cluster.proxy.remove(rule)))
        timer.start()
        try:
            attempts = cluster.janitor_delete(cluster.a, repo, d)
        finally:
            timer.cancel()
            cluster.proxy.release_all(rule)
            cluster.proxy.remove(rule)
        pushes = [(late, push.result(120))]
    problems = check_end_state(cluster, repo, d, seeded, "other", x, pushes=pushes,
                               moved=moved, attempts=attempts)
    return fired, [f"[{route} paused before its storage call #{k}] {p}" for p in problems]


def _sweep(cluster, label, route, limit):
    problems, misses = [], 0
    for k in range(1, limit + 1):
        fired, found = _sweep_case(cluster, label, k, route)
        problems += found
        misses = 0 if fired else misses + 1
        if misses >= 2:
            break
    return problems


def test_deleter_paused_at_each_step_while_pushes_run(cluster):
    problems = _sweep(cluster, "ksweep", "a", 40)
    assert not problems, "\n".join(problems)


def test_pusher_paused_at_each_step_while_delete_runs(cluster):
    problems = _sweep(cluster, "jsweep", "b", 16)
    assert not problems, "\n".join(problems)


def _signal_a(cluster, sig):
    try:
        os.killpg(cluster.a.proc.pid, sig)
    except ProcessLookupError:
        pass


def _stalled_delete_case(cluster, k):
    """Replica a stalls with its k-th storage write of a digest DELETE still in flight. The
    janitor's retry finishes the delete through b, a rollback job re-tags the digest through b,
    and then a resumes and its held write lands."""
    repo, (m, d), (mx, x), seeded = cluster.seed_case(f"stale{k}", 3)
    rule = cluster.rule("a", "hold", MUTATING, None, nth=k)
    first = cluster.a.async_(cluster.a.delete, repo, d, timeout=180)
    if not wait_fired_or_done(rule, first, 10):
        cluster.proxy.release_all(rule)
        cluster.proxy.remove(rule)
        first.result(60)
        return False, []
    _signal_a(cluster, signal.SIGSTOP)
    try:
        attempts = cluster.janitor_delete(cluster.b, repo, d)
        pushes = [("stable", cluster.b.push(repo, "stable", m, timeout=120))]
    finally:
        _signal_a(cluster, signal.SIGCONT)
        cluster.proxy.release_all(rule)
        cluster.proxy.remove(rule)
    cluster.note(f"stalled DELETE on a answered {first.result(200).status}")
    problems = check_end_state(cluster, repo, d, seeded, "other", x, pushes=pushes,
                               attempts=attempts)
    return True, [f"[a stalled at its storage write #{k}] {p}" for p in problems]


def test_stalled_deleter_resumes_after_retry_and_retag(cluster):
    problems, misses = [], 0
    deadline = time.time() + STALE_SWEEP_BUDGET
    for k in range(1, 13):
        fired, problems = _stalled_delete_case(cluster, k)
        misses = 0 if fired else misses + 1
        if problems or misses >= 2 or time.time() > deadline:
            break
    assert not problems, "\n".join(problems)
