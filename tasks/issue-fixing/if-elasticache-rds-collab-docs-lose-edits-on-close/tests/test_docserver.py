"""Graded scenarios. Each closes a document on a docserver while another writer of the same row (the
harness peer, playing another instance, or doccompact) is at work, and checks the two things the
fleet depends on:

- the docserver's acknowledged edits are in the row by the time it reports the document unloaded;
- no committed version of the row ever drops an edit an earlier committed version held.
"""

from __future__ import annotations

import random
import threading
import time

from harness import (
    BARRIER_TIMEOUT_S,
    HELD_LOCK_PX_MS,
    LOCK_PX_MS,
    PEER_TX_MIN_HOLD_S,
    QUIESCE_S,
    UNLOAD_DEADLINE_S,
    VALKEY_OUTAGE_S,
    Background,
    Compactor,
    PeerLock,
    PeerTx,
    log,
)


def _outcome_problems(h, instance, doc, acked_here, closed_at, peer=None):
    problems = []
    unloaded = h.wait_unloaded(instance, doc, closed_at)
    if peer is not None:
        peer.join(30)
        if peer.error is not None:
            problems.append(f"concurrent writer failed: {peer.error!r}")
    if unloaded is None:
        problems.append(f"{doc} still loaded on {instance} {UNLOAD_DEADLINE_S:.0f} s after its last close")
    else:
        elapsed, row_edits = unloaded
        missing = sorted(acked_here - row_edits)
        if missing:
            problems.append(f"{instance} reported {doc} unloaded after {elapsed:.2f} s; the row then lacked acknowledged edits {missing}")
    problems += h.history_problems(doc)
    _, final = h.row(doc)
    missing = sorted(h.acked[doc] - final)
    if missing:
        problems.append(f"{doc}: final row lacks acknowledged edits {missing}")
    return problems


def _deterministic(h, label, lock_px_ms, *, min_hold_s=0.0, terminate=False):
    h.ensure_docservers()
    doc = h.new_doc(label)
    acked_here: set[str] = set()
    h.open("I1", doc)
    for n in range(1, 6):
        h.edit("I1", doc, f"{doc}:I1:{n}", acked_here)
    lock = PeerLock(h, h.lock_key(doc), lock_px_ms)
    assert lock.held, "harness could not take the lock key (held by someone else before the scenario)"
    tx = PeerTx(h, doc)

    def peer():
        blocked = tx.wait_blocked(BARRIER_TIMEOUT_S)
        log(f"{doc}: docserver backends waiting on the peer: {blocked}")
        if terminate and blocked:
            with h.engines.master_conn() as conn:
                for pid in blocked:
                    conn.execute("SELECT pg_terminate_backend(%s)", (pid,))
            log(f"{doc}: terminated {blocked}")
        remaining = min_hold_s - (time.monotonic() - tx.started)
        if remaining > 0:
            time.sleep(remaining)
        tx.commit_edit(f"{doc}:peer:1")
        lock.release()

    writer = Background(peer)
    h.edit("I1", doc, f"{doc}:I1:6", acked_here)
    closed_at = h.close("I1", doc)
    problems = _outcome_problems(h, "I1", doc, acked_here, closed_at, writer)
    time.sleep(QUIESCE_S)
    assert not problems, "\n".join(problems)


def test_close_while_another_instance_holds_the_store_lock(h):
    _deterministic(h, "held", HELD_LOCK_PX_MS)


def test_close_while_the_lock_holder_outlives_its_lock(h):
    _deterministic(h, "expiring", LOCK_PX_MS, min_hold_s=PEER_TX_MIN_HOLD_S)


def test_close_when_the_database_connection_is_reset_mid_store(h):
    _deterministic(h, "reset", LOCK_PX_MS, min_hold_s=PEER_TX_MIN_HOLD_S, terminate=True)


COMPACT_HOLD_S = 4.0
LONG_COMPACT_HOLD_S = 8.5  # a long pass, still inside doccompact's 10 s PX
RDS_REFUSAL_S = 2.5


def test_close_while_compaction_holds_the_lock(h):
    h.ensure_docservers()
    doc = h.new_doc("compacting")
    acked_here: set[str] = set()
    h.open("I1", doc)
    for n in range(1, 6):
        h.edit("I1", doc, f"{doc}:I1:{n}", acked_here)
    compactor = Compactor(h, doc)
    assert compactor.try_acquire(), "harness could not take the lock key (held by someone else before the scenario)"
    job = Background(compactor.run, LONG_COMPACT_HOLD_S)
    h.edit("I1", doc, f"{doc}:I1:6", acked_here)
    closed_at = h.close("I1", doc)
    problems = _outcome_problems(h, "I1", doc, acked_here, closed_at, job)
    time.sleep(QUIESCE_S)
    assert not problems, "\n".join(problems)


def test_close_while_rds_refuses_connections_and_compaction_is_due(h):
    h.ensure_docservers()
    doc = h.new_doc("rdsdown")
    acked_here: set[str] = set()
    h.open("I1", doc)
    for n in range(1, 6):
        h.edit("I1", doc, f"{doc}:I1:{n}", acked_here)
    h.pg_proxy.refuse()
    refused_at = time.monotonic()
    problems = []
    try:
        h.edit("I1", doc, f"{doc}:I1:6", acked_here)
        closed_at = h.close("I1", doc)
        time.sleep(0.3)
        compactor = Compactor(h, doc)
        job = Background(compactor.run, COMPACT_HOLD_S - 1, 8.0)
        time.sleep(max(0.0, RDS_REFUSAL_S - (time.monotonic() - refused_at)))
        h.pg_proxy.forward()
        problems = _outcome_problems(h, "I1", doc, acked_here, closed_at, job)
    finally:
        h.pg_proxy.forward()
    time.sleep(QUIESCE_S)
    assert not problems, "\n".join(problems)


def test_close_during_a_valkey_outage(h):
    h.ensure_docservers()
    doc = h.new_doc("outage")
    acked_here: set[str] = set()
    h.open("I1", doc)
    for n in range(1, 6):
        h.edit("I1", doc, f"{doc}:I1:{n}", acked_here)
    h.proxy.refuse()
    outage_started = time.monotonic()
    health_failures: list[str] = []
    stop = threading.Event()

    def health():
        while not stop.is_set():
            for name, server in h.instances.items():
                try:
                    status, _ = server.request("GET", "/healthz", timeout=2)
                except Exception as error:
                    status = repr(error)
                if status != 200:
                    health_failures.append(f"{name} /healthz -> {status}")
            stop.wait(0.5)

    poller = threading.Thread(target=health, daemon=True)
    poller.start()
    problems = []
    try:
        tx = PeerTx(h, doc)

        def peer():
            blocked = tx.wait_blocked(BARRIER_TIMEOUT_S)
            log(f"{doc}: docserver backends waiting on the peer: {blocked}")
            tx.commit_edit(f"{doc}:peer:1")

        writer = Background(peer)
        h.edit("I1", doc, f"{doc}:I1:6", acked_here)
        closed_at = h.close("I1", doc)
        problems = _outcome_problems(h, "I1", doc, acked_here, closed_at, writer)
    finally:
        remaining = VALKEY_OUTAGE_S - (time.monotonic() - outage_started)
        if remaining > 0:
            time.sleep(remaining)
        stop.set()
        poller.join(5)
        h.proxy.forward()
        time.sleep(QUIESCE_S)
    if health_failures:
        problems.append(f"/healthz failed during the Valkey outage: {health_failures[:5]}")
    assert not problems, "\n".join(problems)


SOAK_ROUNDS = 20
SOAK_DOCS = 4


def test_soak_two_instances_and_a_peer(h):
    h.ensure_docservers()
    rng = random.Random()
    docs = [h.new_doc(f"soak{i}") for i in range(SOAK_DOCS)]
    connections = {(inst, doc): 0 for inst in h.instances for doc in docs}
    acked_by = {(inst, doc): set() for inst in h.instances for doc in docs}
    problems: list[str] = []
    counter = iter(range(1, 1_000_000))
    peer_edits = iter(range(1, 1_000_000))
    guard = threading.Lock()

    def close_and_check(doc, closers, peer):
        closed = {}
        for inst in closers:
            for _ in range(connections[(inst, doc)]):
                closed[inst] = h.close(inst, doc)
            connections[(inst, doc)] = 0
        for inst, closed_at in closed.items():
            unloaded = h.wait_unloaded(inst, doc, closed_at)
            if unloaded is None:
                problems.append(f"{doc} still loaded on {inst} {UNLOAD_DEADLINE_S:.0f} s after its last close")
                continue
            elapsed, row_edits = unloaded
            missing = sorted(acked_by[(inst, doc)] - row_edits)
            if missing:
                problems.append(f"{inst} reported {doc} unloaded after {elapsed:.2f} s; the row then lacked acknowledged edits {missing}")
        if peer is not None:
            peer.join(30)
            if peer.error is not None:
                problems.append(f"peer instance failed on {doc}: {peer.error!r}")

    def one_round(doc, round_no):
        try:
            for inst in rng.sample(list(h.instances), rng.randint(1, 2)):
                if connections[(inst, doc)] == 0 or rng.random() < 0.3:
                    h.open(inst, doc)
                    connections[(inst, doc)] += 1
            open_on = [inst for inst in h.instances if connections[(inst, doc)] > 0]
            lock = PeerLock(h, h.lock_key(doc), 2000) if rng.random() < 0.5 else None
            peer = None
            if rng.random() < 0.5:
                tx = PeerTx(h, doc)
                hold = rng.uniform(0.3, 1.5)

                def commit(tx=tx, hold=hold):
                    tx.wait_blocked(hold)
                    with guard:
                        number = next(peer_edits)
                    tx.commit_edit(f"{doc}:peer:{number}")

                peer = Background(commit)
            for _ in range(rng.randint(1, 3)):
                inst = rng.choice(open_on)
                with guard:
                    edit_id = f"{doc}:{inst}:{next(counter)}"
                h.edit(inst, doc, edit_id, acked_by[(inst, doc)])
            closers = rng.sample(open_on, rng.randint(1, len(open_on)))
            close_and_check(doc, closers, peer)
            if lock is not None:
                lock.release()
        except AssertionError as error:
            problems.append(str(error))

    for round_no in range(SOAK_ROUNDS):
        threads = [threading.Thread(target=one_round, args=(doc, round_no)) for doc in docs]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(120)
        if len(problems) > 20:
            break
    for doc in docs:
        open_on = [inst for inst in h.instances if connections[(inst, doc)] > 0]
        if open_on:
            close_and_check(doc, open_on, None)
    for doc in docs:
        problems += h.history_problems(doc)
        _, final = h.row(doc)
        missing = sorted(h.acked[doc] - final)
        if missing:
            problems.append(f"{doc}: final row lacks acknowledged edits {missing}")
    assert not problems, "\n".join(problems[:30])


def test_lock_keys_held_by_other_instances_were_left_alone(h):
    time.sleep(0.2)
    assert not h.monitor.violations, "\n".join(h.monitor.violations[:10])


def test_docservers_stayed_up(h):
    crashes = [crash for server in h.instances.values() for crash in server.crashes]
    crashes += [f"{name} is not running" for name, server in h.instances.items() if not server.alive()]
    assert not crashes, "; ".join(crashes)
