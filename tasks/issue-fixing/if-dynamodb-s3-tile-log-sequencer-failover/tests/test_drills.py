"""Verifier drills for the tile-log worker.

Three drills, each in a fresh namespace (its own table, bucket and
audit prefix), each grading real DynamoDB and S3 state independently
of anything the workers report.  Nothing here fails fast: every drill
records its outcomes and asserts at the end, so one failed milestone
does not hide the rest.  Submissions carry a batch of entries; one
submission is one checkpoint, so a committed tree can grow by several
entries at once and the coordinates it covers can include a tile that
an earlier size only started.

  Drill 1 — a writer's record write commits while its response is
  withheld and a second writer holding the same parent and a different
  batch is parked.  A replacement must recover the committed tree from
  the services alone and the log must grow on it; only then does the
  first writer learn its commit, with the log already past it; the
  parked writer must finish without touching the committed tree and
  then serve the recovered log itself.

  Drill 2 — both writers' fully assembled record writes are parked at
  the wire; one is let through and its writer killed, the replacement
  recovers, and the other writer's original request bytes are then let
  through unchanged.

  Drill 3 — a writer is killed with its record write never forwarded;
  the replacement must place different entries at the same coordinates
  and then grow the log across the tile boundary the abandoned attempt
  stopped inside.
"""

import base64
import os
import secrets
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import harness as H  # noqa: E402


def b64d(s):
    return base64.b64decode(s)


def wait_for(predicate, timeout, interval=H.POLL_INTERVAL_S):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


def start_and_load(checks, worker, expected_checkpoint, label,
                   deadline=H.STARTUP_DEADLINE_S):
    """Start a worker, wait for its startup pause, check what it loaded."""
    t0 = time.monotonic()
    worker.start()
    event = worker.hub.next("startup_complete", H.with_slack(deadline))
    ok = event is not None
    checks.expect("%s reached its startup pause" % label, ok,
                  worker.tail() if not ok else "")
    if not ok:
        return None
    loaded = b64d(event["checkpoint"])
    checks.expect("%s loads the committed head" % label,
                  loaded == expected_checkpoint,
                  "loaded %s, record is %s"
                  % (loaded.hex()[:16], expected_checkpoint.hex()[:16]))
    worker.hub.release(event)
    worker.wait_listening(H.with_slack(deadline))
    return time.monotonic() - t0


def expect_recovery(checks, ns, worker, expected_checkpoint, expected_leaves,
                    label, deadline=H.RECOVERY_DEADLINE_S):
    """Start a replacement and demand the committed tree be published."""
    t0 = time.monotonic()
    worker.start()
    event = worker.hub.next("startup_complete", H.with_slack(deadline))
    elapsed = time.monotonic() - t0
    checks.expect("%s replacement reaches its startup pause" % label,
                  event is not None, worker.tail() if event is None else "")
    if event is None:
        return
    checks.expect("%s replacement publishes within the deadline" % label,
                  elapsed <= H.with_slack(deadline), "%.2fs" % elapsed)
    problems = ns.verify_public_tree(expected_checkpoint, expected_leaves)
    checks.expect("%s committed tree fully published after recovery"
                  % label, not problems, "; ".join(problems))
    loaded = b64d(event["checkpoint"])
    checks.expect("%s replacement reports the committed head" % label,
                  loaded == expected_checkpoint,
                  "loaded %s" % loaded.hex()[:16])
    worker.hub.release(event)
    worker.wait_listening(H.with_slack(deadline))


def expect_stale_finished(checks, worker, box, label):
    """The stale attempt must end without a committed response."""
    event = worker.hub.next("before_reply",
                            H.with_slack(H.CONFLICT_DEADLINE_S))
    if event is None:
        response, error = worker.wait_response(
            box, H.with_slack(H.CONFLICT_DEADLINE_S))
        checks.expect("%s stale attempt returns no committed response"
                      % label,
                      not (response and response.get("status")
                           == "committed"), str(response or error))
        return response
    committed = event.get("status") == "committed"
    worker.hub.release(event)
    response, error = worker.wait_response(box, H.with_slack(
        H.CONFLICT_DEADLINE_S))
    checks.expect("%s stale attempt returns no committed response" % label,
                  not committed and not (
                      response and response.get("status") == "committed"),
                  "pause said %r, response %r" % (event.get("status"),
                                                  response or error))
    return response


def expect_progress(checks, ns, worker, entries, leaves_before,
                    expected_checkpoint, expected_leaves, label):
    """Submit a fresh batch; it must commit and be publicly resolvable."""
    box = worker.submit_async("req-fresh-%s" % label, entries)
    event = worker.hub.next("before_reply",
                            H.with_slack(H.PROGRESS_DEADLINE_S))
    if event is None or event.get("status") != "committed":
        if event is not None:
            worker.hub.release(event)
        worker.wait_response(box, 3)
        checks.expect("%s fresh submission commits" % label, False,
                      worker.tail())
        return
    # The worker is paused just before answering: the entries must
    # already be publicly resolvable at this instant.
    problems = ns.verify_public_tree(expected_checkpoint, expected_leaves)
    checks.expect("%s entries publicly resolvable before the response" % label,
                  not problems, "; ".join(problems))
    worker.hub.release(event)
    response, error = worker.wait_response(
        box, H.with_slack(H.PROGRESS_DEADLINE_S))
    ok = bool(response and response.get("status") == "committed")
    checks.expect("%s fresh submission commits" % label, ok,
                  str(response or error))
    if ok:
        checks.expect("%s receipt index points at the new entries" % label,
                      response.get("index") == len(leaves_before),
                      "index %r" % response.get("index"))
    problems = ns.verify_public_tree(expected_checkpoint, expected_leaves)
    checks.expect("%s final tree published and consistent" % label,
                  not problems, "; ".join(problems))
    checks.expect("%s record at the final head" % label,
                  ns.record() == expected_checkpoint)


def expect_late_answer(checks, ns, worker, box, expected_checkpoint,
                       expected_leaves, first_index, label):
    """Deliver a fence response the world has already moved past.

    The worker's record write succeeded long ago; everything committed
    since is published.  When it finally learns its own outcome it will
    publish what it won — that publication may not undo any of it.
    """
    worker.relay.deliver_response()
    event = worker.hub.next("before_reply",
                            H.with_slack(H.PROGRESS_DEADLINE_S))
    checks.expect("%s withheld writer learns its commit at the reply pause"
                  % label,
                  event is not None and event.get("status") == "committed",
                  "pause said %r" % (event and event.get("status")))
    if event is not None and event.get("status") == "committed":
        checks.expect("%s late receipt names the first index it committed"
                      % label, event.get("index") == first_index,
                      "index %r" % event.get("index"))
    problems = ns.verify_public_tree(expected_checkpoint, expected_leaves)
    checks.expect("%s later tree still published while the late writer "
                  "answers" % label, not problems, "; ".join(problems))
    if event is not None:
        worker.hub.release(event)
    response, error = worker.wait_response(
        box, H.with_slack(H.PROGRESS_DEADLINE_S))
    checks.expect("%s withheld submission answered committed at its index"
                  % label,
                  bool(response and response.get("status") == "committed"
                       and response.get("index") == first_index),
                  str(response or error))
    time.sleep(H.QUIET_WINDOW_S)
    problems = ns.verify_public_tree(expected_checkpoint, expected_leaves)
    checks.expect("%s later tree unchanged after the late answer" % label,
                  not problems, "; ".join(problems))
    checks.expect("%s record unchanged by the late answer" % label,
                  ns.record() == expected_checkpoint)


# ------------------------------------------------------------- drills


def drill_failover_with_duplicate(services):
    checks = H.Checks("drill1")
    ns = H.Namespace(services, "failover", "drill-failover.example.net")
    batch_a = [secrets.token_bytes(24) for _ in range(4)]
    batch_b = [secrets.token_bytes(24) for _ in range(2)]
    batch_c = [secrets.token_bytes(24) for _ in range(4)]
    batch_d = [secrets.token_bytes(24) for _ in range(2)]
    leaves = batch_a + batch_b + batch_d
    assert len(set(leaves)) == len(leaves)
    assert set(batch_c).isdisjoint(leaves)
    leaves_ab = batch_a + batch_b
    c1 = H.format_checkpoint(ns.origin, 4, H.tree_root(batch_a), 1)
    c2 = H.format_checkpoint(ns.origin, 6, H.tree_root(leaves_ab), 2)
    c3 = H.format_checkpoint(ns.origin, 8, H.tree_root(leaves), 3)

    ports = H.free_ports(3)
    stale = H.Worker(ns, "w2", ports[1])
    winner = H.Worker(ns, "w1", ports[0], relay_mode="withhold")
    replacement = H.Worker(ns, "r", ports[2])
    workers = [stale, winner, replacement]
    try:
        # The stale writer loads the shared parent and fixes its attempt.
        start_and_load(checks, stale, ns.c0, "stale writer")
        stale.hub.auto.discard("batch_assembled")
        box_stale = stale.submit_async("req-stale", batch_c)
        parked = stale.hub.next("batch_assembled",
                                H.with_slack(H.BARRIER_DEADLINE_S))
        checks.expect("stale attempt parked before its storage writes",
                      parked is not None, stale.tail())
        if parked is not None:
            checks.expect("stale attempt fixes the current parent",
                          b64d(parked["parent"]) == ns.c0
                          and parked["index"] == 0,
                          "parent %s index %r"
                          % (parked["parent"][:16], parked.get("index")))
            checks.expect("stale candidate carries the other entries",
                          parked["candidate_size"] == len(batch_c)
                          and bytes.fromhex(parked["candidate_root"])
                          == H.tree_root(batch_c),
                          "size %r root %s"
                          % (parked.get("candidate_size"),
                             parked.get("candidate_root", "")[:16]))

        # The winning writer commits; the response never reaches it.
        start_and_load(checks, winner, ns.c0, "winning writer")
        box_winner = winner.submit_async("req-winner", batch_a)
        armed = winner.hub.next("batch_assembled",
                                H.with_slack(H.BARRIER_DEADLINE_S))
        checks.expect("winning attempt fixes the current parent",
                      armed is not None
                      and b64d(armed["parent"]) == ns.c0
                      and armed["index"] == 0)
        checks.expect("winning candidate carries its batch",
                      armed is not None
                      and armed["candidate_size"] == len(batch_a)
                      and bytes.fromhex(armed["candidate_root"])
                      == H.tree_root(batch_a))
        checks.expect("winning record write reached the service",
                      winner.relay.wait_arrived(
                          H.with_slack(H.BARRIER_DEADLINE_S)))
        checks.expect("winning record write succeeded",
                      winner.relay.wait_responded(
                          H.with_slack(H.BARRIER_DEADLINE_S))
                      and winner.relay.responses
                      and winner.relay.responses[0]["status"] == 200,
                      str(winner.relay.responses))
        got = ns.record()
        checks.expect("record holds the committed candidate at the gate",
                      got == c1,
                      "record %s" % (got.hex()[:16] if got else None))
        ns.audit("after-commit", {
            "record": got,
            "relay": winner.relay.responses,
        })
        response, _ = winner.wait_response(box_winner, 1.0)
        checks.expect("no reply has reached the committed writer yet",
                      response is None, str(response))

        # The replacement recovers from the services alone, and the log
        # grows on the recovered tree before the writer learns anything.
        expect_recovery(checks, ns, replacement, c1, batch_a,
                        "drill1")
        ns.audit("after-recovery", {"record": ns.record()})
        expect_progress(checks, ns, replacement, batch_b, batch_a,
                        c2, leaves_ab, "drill1")
        ns.audit("after-progress", {"record": ns.record()})

        # Now the withheld outcome reaches a writer whose commit is
        # already two checkpoints behind the published log.
        expect_late_answer(checks, ns, winner, box_winner, c2, leaves_ab,
                           0, "drill1")
        ns.audit("after-late-answer", {"record": ns.record()})

        # The stale writer finishes; the committed tree must not move.
        if parked is not None:
            stale.hub.release(parked)
        else:
            stale.hub.release_all("batch_assembled")
        expect_stale_finished(checks, stale, box_stale, "drill1")
        got = ns.record()
        checks.expect("record unchanged by the stale attempt", got == c2,
                      "record %s" % (got.hex()[:16] if got else None))
        problems = ns.verify_public_tree(c2, leaves_ab)
        checks.expect("committed tree bytes unchanged by the stale attempt",
                      not problems, "; ".join(problems))
        time.sleep(H.QUIET_WINDOW_S)
        problems = ns.verify_public_tree(c2, leaves_ab)
        checks.expect("committed tree still unchanged after the quiet "
                      "window", not problems, "; ".join(problems))
        ns.audit("after-stale", {"record": ns.record()})

        # The worker that lost the fence serves the recovered log too.
        stale.hub.auto.add("batch_assembled")
        expect_progress(checks, ns, stale, batch_d, leaves_ab,
                        c3, leaves, "drill1")
        ns.audit("after-stale-progress", {"record": ns.record()})
    finally:
        for worker in workers:
            worker.shutdown()
    return checks


def drill_parked_record_write(services):
    checks = H.Checks("drill2")
    ns = H.Namespace(services, "parked", "drill-parked.example.net")
    batch_a = [secrets.token_bytes(24) for _ in range(4)]
    batch_b = [secrets.token_bytes(24) for _ in range(2)]
    batch_c = [secrets.token_bytes(24) for _ in range(4)]
    batch_d = [secrets.token_bytes(24) for _ in range(2)]
    leaves = batch_a + batch_b + batch_d
    assert len(set(leaves)) == len(leaves)
    assert set(batch_c).isdisjoint(leaves)
    leaves_ab = batch_a + batch_b
    c1 = H.format_checkpoint(ns.origin, 4, H.tree_root(batch_a), 1)
    c2 = H.format_checkpoint(ns.origin, 6, H.tree_root(leaves_ab), 2)
    c3 = H.format_checkpoint(ns.origin, 8, H.tree_root(leaves), 3)

    ports = H.free_ports(3)
    winner = H.Worker(ns, "w1", ports[0], relay_mode="park_withhold")
    stale = H.Worker(ns, "w2", ports[1], relay_mode="park")
    replacement = H.Worker(ns, "r", ports[2])
    workers = [winner, stale, replacement]
    try:
        start_and_load(checks, winner, ns.c0, "drill2 first writer")
        start_and_load(checks, stale, ns.c0, "drill2 second writer")

        # Both assemble a complete record write against the shared
        # parent; both are parked at the wire, after their reads.
        box_winner = winner.submit_async("req-winner", batch_a)
        box_stale = stale.submit_async("req-stale", batch_c)
        checks.expect("first writer's assembled record write parked",
                      winner.relay.wait_arrived(
                          H.with_slack(H.BARRIER_DEADLINE_S)))
        checks.expect("second writer's assembled record write parked",
                      stale.relay.wait_arrived(
                          H.with_slack(H.BARRIER_DEADLINE_S)))
        got = ns.record()
        checks.expect("record still at the shared parent while parked",
                      got == ns.c0)

        # Let the first writer's own request bytes through; its writer
        # dies before it learns the outcome.
        winner.relay.forward()
        checks.expect("first writer's record write succeeded",
                      winner.relay.wait_responded(
                          H.with_slack(H.BARRIER_DEADLINE_S))
                      and winner.relay.responses
                      and winner.relay.responses[0]["status"] == 200,
                      str(winner.relay.responses))
        got = wait_for(lambda: ns.record() if ns.record() != ns.c0 else None,
                       H.with_slack(H.BARRIER_DEADLINE_S))
        checks.expect("record holds the first writer's candidate",
                      got == c1, "record %s"
                      % (got.hex()[:16] if got else None))
        ns.audit("after-first-release", {"record": got})
        winner.kill()
        wait_for(lambda: not winner.alive(), 5)

        expect_recovery(checks, ns, replacement, c1, batch_a, "drill2")
        ns.audit("after-recovery", {"record": ns.record()})

        # Release the second writer's original, already-assembled bytes.
        stale.relay.forward()
        checks.expect("stale assembled record write answered by the "
                      "service",
                      stale.relay.wait_responded(
                          H.with_slack(H.BARRIER_DEADLINE_S))
                      and bool(stale.relay.responses))
        rejected = bool(stale.relay.responses) and \
            stale.relay.responses[0].get("conditional_failed")
        checks.expect("stale assembled record write rejected", rejected,
                      str(stale.relay.responses))
        stale.relay.deliver_response()
        got = ns.record()
        checks.expect("record unchanged by the stale assembled write",
                      got == c1, "record %s"
                      % (got.hex()[:16] if got else None))
        expect_stale_finished(checks, stale, box_stale, "drill2")
        time.sleep(H.QUIET_WINDOW_S)
        problems = ns.verify_public_tree(c1, batch_a)
        checks.expect("committed tree still intact after the stale "
                      "assembled write", not problems, "; ".join(problems))
        ns.audit("after-stale", {"record": ns.record()})

        expect_progress(checks, ns, replacement, batch_b, batch_a,
                        c2, leaves_ab, "drill2")
        ns.audit("after-progress", {"record": ns.record()})
        expect_progress(checks, ns, stale, batch_d, leaves_ab,
                        c3, leaves, "drill2")
        ns.audit("after-stale-progress", {"record": ns.record()})
    finally:
        for worker in workers:
            worker.shutdown()
    return checks


def drill_abandoned_attempt(services):
    checks = H.Checks("drill3")
    ns = H.Namespace(services, "abandoned", "drill-abandoned.example.net")
    batch_e = [secrets.token_bytes(24) for _ in range(3)]
    batch_f = [secrets.token_bytes(24) for _ in range(3)]
    batch_g = [secrets.token_bytes(24) for _ in range(3)]
    leaves = batch_f + batch_g
    assert len(set(batch_e + leaves)) == len(batch_e + leaves)
    c1 = H.format_checkpoint(ns.origin, 3, H.tree_root(batch_f), 1)
    c2 = H.format_checkpoint(ns.origin, 6, H.tree_root(leaves), 2)

    ports = H.free_ports(2)
    abandoned = H.Worker(ns, "w1", ports[0], relay_mode="suppress")
    replacement = H.Worker(ns, "r", ports[1])
    workers = [abandoned, replacement]
    try:
        start_and_load(checks, abandoned, ns.c0, "drill3 writer")

        # The writer's record write is captured and never forwarded.
        box_abandoned = abandoned.submit_async("req-abandoned", batch_e)
        armed = abandoned.hub.next("batch_assembled",
                                   H.with_slack(H.BARRIER_DEADLINE_S))
        checks.expect("abandoned attempt assembled", armed is not None,
                      abandoned.tail())
        checks.expect("abandoned record write captured, never forwarded",
                      abandoned.relay.wait_arrived(
                          H.with_slack(H.BARRIER_DEADLINE_S))
                      and not abandoned.relay.responses)
        checks.expect("record untouched by the abandoned attempt",
                      ns.record() == ns.c0)
        abandoned.kill()
        wait_for(lambda: not abandoned.alive(), 5)
        response, _ = abandoned.wait_response(box_abandoned, 1.0)
        checks.expect("no committed response for the abandoned entries",
                      not (response and response.get("status")
                           == "committed"))
        ns.audit("after-abandon", {"record": ns.record()})

        # The replacement must place other entries at those same
        # coordinates, and then take the log past the tile boundary.
        start_and_load(checks, replacement, ns.c0, "drill3 replacement")
        problems = ns.verify_public_tree(ns.c0, [])
        checks.expect("empty tree still published before the new entries",
                      not problems, "; ".join(problems))
        expect_progress(checks, ns, replacement, batch_f, [],
                        c1, batch_f, "drill3")
        expect_progress(checks, ns, replacement, batch_g, batch_f,
                        c2, leaves, "drill3")
        checks.expect("final size is 6", parse_size(ns) == 6,
                      "size %r" % parse_size(ns))
        ns.audit("after-progress", {"record": ns.record()})
    finally:
        for worker in workers:
            worker.shutdown()
    return checks


def parse_size(ns):
    record = ns.record()
    if record is None:
        return None
    return H.parse_checkpoint(record)["size"]


# --------------------------------------------------------------- tests


def _services():
    return H.Services()


def test_00_environment_ready():
    services = _services()
    if not services.healthy():
        print("[environment] the emulator is not answering on "
              "http://localhost:4566", flush=True)
    assert services.healthy(), "[environment] emulator not healthy"


def _run(group, drill):
    """Run one drill; an unexpected crash is a failed drill, not a
    verifier without a score."""
    checks = H.Checks(group)
    try:
        checks = drill(_services())
    except Exception as exc:  # noqa: BLE001 - record, then score
        import traceback
        traceback.print_exc()
        checks.record("%s harness error" % group, False, repr(exc))
    bad = checks.summary()
    assert bad == 0, "%d failed checks in %s" % (bad, group)


def test_10_failover_with_duplicate_writer():
    _run("drill1", drill_failover_with_duplicate)


def test_20_parked_record_write_race():
    _run("drill2", drill_parked_record_write)


def test_30_abandoned_attempt_before_the_record():
    _run("drill3", drill_abandoned_attempt)
