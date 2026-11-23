"""Compose-review under worker loss and SQS redelivery.

Every scenario gets its own queue, tables and worker pool. Workers are killed by
their relay right after a committed write, before they see its reply (or frozen
there and resumed later); the pool replaces them after a second; SQS redelivers
after the 6 s visibility timeout. A stand-in for the user-review service consumes
its queue throughout; moderation takes some finished reviews down.
"""
import random
import time

import pytest

import harness as h


def _interrupted(req):
    return any(n.startswith("planned ") and "never" not in n for n in req.notes)


def _sequenced_case(tag, **schedule):
    with h.deployment(tag) as (stack, pool):
        req = h.run_sequenced(stack, pool, **schedule)
        h.wait_complete(stack, [req], req.last_enqueue + h.REQUEST_DEADLINE_S)
        snaps = {req.req_id: stack.snapshot(req)}
        problems = h.settle_and_grade(stack, pool, [req], True, snaps)
        if schedule.get("kill") and not _interrupted(req):
            problems.append(f"{req.req_id}: the planned interruption never happened ({req.notes})")
    assert not problems, "\n".join(problems)


def test_uploads_without_interruption():
    with h.deployment("baseline") as (stack, pool):
        reqs = []
        for _ in range(2):
            req = h.run_sequenced(stack, pool, movie_id="tt0000001")
            h.wait_complete(stack, [req], req.last_enqueue + h.REQUEST_DEADLINE_S)
            reqs.append(req)
        # Moderation takes the first review down once it is on the page.
        stack.take_down(reqs[0])
        snaps = {r.req_id: stack.snapshot(r) for r in reqs}
        # The frontend's late retry of a SendMessage: a part of a finished request, queued again.
        for req in reqs:
            stack.send(req, random.choice(h.PARTS))
        problems = h.settle_and_grade(stack, pool, reqs, True, snaps)
    assert not problems, "\n".join(problems)


def test_first_part_lost_after_its_increment():
    # The retry of the first part runs after the other four have finished.
    _sequenced_case("first-part-lost", kill="counter", kill_at=1)


def test_completing_part_lost_after_its_increment():
    _sequenced_case("completing-part-lost", kill="counter", kill_at=5)


def test_first_part_retried_while_last_part_outstanding():
    # The last part is queued only once the first part's retry has replied.
    _sequenced_case("first-part-retried-early", kill="counter", kill_at=1, hold_last=True)


def test_completing_part_lost_during_compose():
    _sequenced_case("completing-part-lost-mid-compose", kill="compose", kill_at=5)


def test_completing_part_lost_after_cleanup():
    _sequenced_case("completing-part-lost-after-cleanup", kill="cleanup", kill_at=5)


def test_resubmitted_while_completing_part_is_lost():
    with h.deployment("resubmitted") as (stack, pool):
        first = h.run_sequenced(stack, pool, kill="counter", kill_at=5)
        # No complete set of replies yet: the frontend resubmits the review as a new request,
        # which runs while the first request's completing part waits for redelivery.
        again = h.run_sequenced(stack, pool, req=stack.resubmit(first))
        reqs = [first, again]
        h.wait_complete(stack, reqs, max(r.last_enqueue for r in reqs) + h.REQUEST_DEADLINE_S)
        snaps = {r.req_id: stack.snapshot(r) for r in reqs}
        problems = h.settle_and_grade(stack, pool, reqs, True, snaps)
        if not _interrupted(first):
            problems.append(f"{first.req_id}: the planned interruption never happened ({first.notes})")
    assert not problems, "\n".join(problems)


def test_rollout_onto_requests_in_flight():
    plan = {}

    def before_switch(stack, logdir):
        # Today's pool finishes one request, records two and four parts of two others, and is reclaimed
        # right after the cleanup of a fourth request's completing upload, before its hand-off and reply.
        plan["done"], plan["two"], plan["four"], plan["cut"] = (stack.new_request() for _ in range(4))
        h.run_current_pool(stack, logdir, [(plan["done"], 5, False), (plan["two"], 2, False),
                                           (plan["four"], 4, False), (plan["cut"], 5, True)])
        # Moderation takes both composed reviews down before the new pool starts.
        for req in (plan["done"], plan["cut"]):
            if not stack.take_down(req):
                raise RuntimeError(f"verifier: {req.req_id}'s review was not listed by the pre-switch pool")

    with h.deployment("rollout", before_switch) as (stack, pool):
        done, two, four, cut = plan["done"], plan["two"], plan["four"], plan["cut"]
        # Frontend retries of parts the old pool already handled, for a finished and an open request.
        stack.send(done, done.order[-1])
        stack.send(done, random.choice(done.order[:-1]))
        stack.send(two, two.order[0])
        # The cut-off upload's message comes back once its visibility timeout has passed.
        h.wait_drained(stack, 2 * h.VISIBILITY_TIMEOUT_S)
        h.run_sequenced(stack, pool, req=two, start=3)
        stack.send(four, four.order[1])
        h.wait_drained(stack, 2 * h.VISIBILITY_TIMEOUT_S)
        h.run_sequenced(stack, pool, req=four, start=5, kill="counter", kill_at=5)
        reqs = [done, two, four, cut]
        h.wait_complete(stack, reqs, max(r.last_enqueue for r in reqs) + h.REQUEST_DEADLINE_S)
        snaps = {r.req_id: stack.snapshot(r) for r in reqs}
        problems = h.settle_and_grade(stack, pool, reqs, True, snaps)
        if not _interrupted(four):
            problems.append(f"{four.req_id}: the planned interruption never happened ({four.notes})")
    assert not problems, "\n".join(problems)


def test_hibernated_workers_resume_after_takedown():
    # Two completing uploads are frozen (not killed): one right after its increment, one right after its
    # first compose write. Each request is finished by a redelivery, moderation takes the review down,
    # and then the frozen workers resume where they stopped.
    with h.deployment("hibernated") as (stack, pool):
        reqs = []
        for point in ("counter", "compose"):
            req = h.run_sequenced(stack, pool, kill=point, kill_at=5, action="freeze")
            h.wait_complete(stack, [req], req.last_enqueue + h.REQUEST_DEADLINE_S)
            stack.take_down(req)
            reqs.append(req)
        snaps = {r.req_id: stack.snapshot(r) for r in reqs}
        pool.thaw_all()
        problems = h.settle_and_grade(stack, pool, reqs, True, snaps)
        problems += [f"{r.req_id}: the planned interruption never happened ({r.notes})"
                     for r in reqs if not _interrupted(r)]
    assert not problems, "\n".join(problems)


CHAOS_REQUESTS = 12
CHAOS_CONCURRENT_REQUESTS = 3
KILL_PROB = 0.25


def test_spot_reclaims_under_concurrent_load():
    rng = random.Random()
    with h.deployment("concurrent-load") as (stack, pool):
        movies = [f"tt{rng.randint(1000000, 9999999)}" for _ in range(3)]
        pool.controller.arm_chaos(KILL_PROB, rng.random())
        reqs, snaps, late = [], {}, []
        for _ in range(CHAOS_REQUESTS // CHAOS_CONCURRENT_REQUESTS):
            batch = [stack.new_request(rng.choice(movies)) for _ in range(CHAOS_CONCURRENT_REQUESTS)]
            # One submission of the batch is resubmitted by the frontend while it is in flight.
            batch.append(stack.resubmit(batch[0]))
            sends = [(r, part) for r in batch for part in h.PARTS]
            rng.shuffle(sends)
            for req, part in sends:
                stack.send(req, part)
            # The frontend re-sends one part of each request (identical body).
            for req in batch:
                time.sleep(rng.uniform(0, 0.8))
                stack.send(req, rng.choice(h.PARTS))
            last = max(r.last_enqueue for r in batch)
            late += h.wait_complete(stack, batch, last + h.REQUEST_DEADLINE_S)
            for req in batch:
                snaps[req.req_id] = stack.snapshot(req)
            # A late frontend retry for one request of the batch, after it finished.
            stack.send(rng.choice(batch), rng.choice(h.PARTS))
            reqs += batch
        pool.controller.disarm()
        problems = h.settle_and_grade(stack, pool, reqs, False, snaps)
        problems += [f"{r.req_id}: not complete within {h.REQUEST_DEADLINE_S}s of its last enqueue" for r in late]
        kills = pool.kill_count()
    assert not problems, f"{kills} workers killed\n" + "\n".join(problems)
