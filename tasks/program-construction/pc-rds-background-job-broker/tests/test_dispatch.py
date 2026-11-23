"""Which job a worker receives next: priorities, scheduling, routing and wake-ups."""

import time

from broker import row, sync


def _drain(worker, result=b""):
    """Grab and complete jobs until NO_JOB; returns the (unique, workload) sequence."""
    order = []
    while (job := worker.grab()) is not None:
        order.append((job[2], job[3]))
        worker.send("WORK_COMPLETE", job[0], result)
    return order


def test_priority_levels_then_submission_order(server):
    client, worker = server.conn(), server.conn()
    for command, unique in (("SUBMIT_JOB_LOW_BG", "low-1"), ("SUBMIT_JOB_BG", "norm-1"), ("SUBMIT_JOB_HIGH_BG", "high-1"),
                            ("SUBMIT_JOB_LOW_BG", "low-2"), ("SUBMIT_JOB_HIGH_BG", "high-2"), ("SUBMIT_JOB_BG", "norm-2")):
        client.submit_ok(command, "prio", unique, unique.encode())
    worker.can_do("prio")
    assert [u for u, _ in _drain(worker)] == [b"high-1", b"high-2", b"norm-1", b"norm-2", b"low-1", b"low-2"]
    sync(worker)
    assert server.rows() == []


def test_duplicate_does_not_change_queue_position(server):
    client, worker = server.conn(), server.conn()
    client.submit_ok("SUBMIT_JOB_LOW_BG", "pos", "x", b"x-low")
    client.submit_ok("SUBMIT_JOB_BG", "pos", "y", b"y-normal")
    client.submit_ok("SUBMIT_JOB_HIGH_BG", "pos", "x", b"x-high")
    server.expect_rows(row("x", "pos", 2, b"x-low"), row("y", "pos", 1, b"y-normal"))
    worker.can_do("pos")
    assert _drain(worker) == [(b"y", b"y-normal"), (b"x", b"x-low")]


def test_foreground_and_background_share_queues(server):
    fg, bg, worker = server.conn(), server.conn(), server.conn()
    fg_low = fg.submit_ok("SUBMIT_JOB_LOW", "shared", "f-low", b"1")
    bg.submit_ok("SUBMIT_JOB_BG", "shared", "b-norm", b"2")
    fg_high = fg.submit_ok("SUBMIT_JOB_HIGH", "shared", "f-high", b"3")
    bg.submit_ok("SUBMIT_JOB_HIGH_BG", "shared", "b-high", b"4")
    fg_norm = fg.submit_ok("SUBMIT_JOB", "shared", "f-norm", b"5")
    worker.can_do("shared")
    assert [u for u, _ in _drain(worker, b"ok")] == [b"f-high", b"b-high", b"b-norm", b"f-norm", b"f-low"]
    received = sorted(fg.recv() for _ in range(3))
    assert received == sorted(("WORK_COMPLETE", h + b"\0ok") for h in (fg_low, fg_high, fg_norm))
    assert bg.nothing_pending()


def test_future_epoch_job_waits_for_its_time(server):
    client, worker, admin = server.conn(), server.conn(), server.admin()
    worker.can_do("sched")
    due = int(time.time()) + 4
    timed = client.submit_ok("SUBMIT_JOB_EPOCH", "sched", "timed", b"T", epoch=due)
    client.submit_ok("SUBMIT_JOB_BG", "sched", "now", b"N")
    client.submit_ok("SUBMIT_JOB_LOW_BG", "sched", "later-low", b"L")
    assert admin.status() == [("sched", 3, 0, 1)]
    assert client.status(timed) == [timed, b"1", b"0", b"0", b"0"]
    assert _drain(worker) == [(b"now", b"N"), (b"later-low", b"L")]
    assert time.time() < due, "test machine too slow for the scheduling window"
    sync(worker)
    server.expect_rows(row("timed", "sched", 1, b"T", due))
    # A sleeping worker is woken once the job becomes due and then receives it.
    while True:
        worker.send("PRE_SLEEP")
        assert worker.recv(timeout=due + 4 - time.time()) == ("NOOP", b"")
        job = worker.grab()
        if job is not None:
            break
        assert time.time() < due, "woken after the due time without receiving the job"
    received = time.time()
    assert due - 1 <= received <= due + 3, (received, due)
    assert job == [timed, b"sched", b"timed", b"T"]
    worker.send("WORK_COMPLETE", timed, b"")
    sync(worker)
    assert server.rows() == []


def test_due_epoch_job_keeps_its_queue_position(server):
    client, worker = server.conn(), server.conn()
    worker.can_do("slot")
    due = int(time.time()) + 3
    client.submit_ok("SUBMIT_JOB_EPOCH", "slot", "epoch", b"E", epoch=due)
    client.submit_ok("SUBMIT_JOB_BG", "slot", "n-1", b"1")
    client.submit_ok("SUBMIT_JOB_BG", "slot", "n-2", b"2")
    client.submit_ok("SUBMIT_JOB_LOW_BG", "slot", "l-1", b"3")
    job = worker.grab()
    assert job[2] == b"n-1" and time.time() < due, "test machine too slow for the scheduling window"
    worker.send("WORK_COMPLETE", job[0], b"")
    while time.time() < due + 1:
        time.sleep(0.1)
    client.submit_ok("SUBMIT_JOB_BG", "slot", "n-3", b"4")
    assert [u for u, _ in _drain(worker)] == [b"epoch", b"n-2", b"n-3", b"l-1"]
    # Once due, epoch jobs run in their submission order, not in order of their times.
    soon = int(time.time()) + 2
    client.submit_ok("SUBMIT_JOB_EPOCH", "slot", "later", b"L", epoch=soon + 1)
    client.submit_ok("SUBMIT_JOB_EPOCH", "slot", "earlier", b"E", epoch=soon)
    client.submit_ok("SUBMIT_JOB_BG", "slot", "plain", b"P")
    while time.time() < soon + 2:
        time.sleep(0.1)
    assert [u for u, _ in _drain(worker)] == [b"later", b"earlier", b"plain"]


def test_past_epoch_job_is_runnable_in_order(server):
    client, worker = server.conn(), server.conn()
    client.submit_ok("SUBMIT_JOB_BG", "past", "first", b"1")
    client.submit_ok("SUBMIT_JOB_EPOCH", "past", "epoch", b"2", epoch=1000)
    client.submit_ok("SUBMIT_JOB_EPOCH", "past", "recent", b"3", epoch=int(time.time()) - 5)
    client.submit_ok("SUBMIT_JOB_BG", "past", "last", b"4")
    worker.can_do("past")
    assert [u for u, _ in _drain(worker)] == [b"first", b"epoch", b"recent", b"last"]


def test_sleeping_worker_is_woken_by_submissions(server):
    client, worker, idle = server.conn(), server.conn(), server.conn()
    worker.can_do("wake")
    idle.can_do("unrelated")
    for sleeper in (worker, idle):
        sleeper.send("PRE_SLEEP")
        sync(sleeper)
    handle = client.submit_ok("SUBMIT_JOB_LOW_BG", "wake", "w-1", b"bg")
    assert worker.recv() == ("NOOP", b"")
    assert worker.grab() == [handle, b"wake", b"w-1", b"bg"]
    worker.send("WORK_COMPLETE", handle, b"")
    worker.send("PRE_SLEEP")
    sync(worker)
    fg = client.submit_ok("SUBMIT_JOB_HIGH", "wake", "w-2", b"fg")
    assert worker.recv() == ("NOOP", b"")
    assert worker.grab()[0] == fg
    assert idle.nothing_pending()
    # A worker announcing sleep while work is waiting is told so at once.
    client.submit_ok("SUBMIT_JOB_BG", "unrelated", "u-1", b"x")
    assert idle.recv() == ("NOOP", b"")
    idle.send("PRE_SLEEP")
    assert idle.recv() == ("NOOP", b"")


def test_jobs_route_by_registered_function(server):
    client, worker = server.conn(), server.conn()
    client.submit_ok("SUBMIT_JOB_BG", "route-a", "a-1", b"a")
    client.submit_ok("SUBMIT_JOB_HIGH_BG", "route-b", "b-1", b"b")
    client.submit_ok("SUBMIT_JOB_BG", "route-c", "c-1", b"c")
    worker.can_do("route-a", "route-c")
    worker.send("CANT_DO", "route-c")
    assert _drain(worker) == [(b"a-1", b"a")]
    worker.can_do("route-b")
    worker.send("RESET_ABILITIES")
    assert worker.grab() is None
    worker.can_do("route-c", "route-b")
    # The high-priority job wins although its function was registered last.
    assert _drain(worker) == [(b"b-1", b"b"), (b"c-1", b"c")]


def _uniques(worker):
    return [u.decode() for u, _ in _drain(worker)]


def test_one_worker_takes_highest_priority_across_functions(server):
    client, worker = server.conn(), server.conn()
    worker.can_do("fn-b", "fn-a")
    client.submit_ok("SUBMIT_JOB_BG", "fn-a", "a-1", b"x")
    client.submit_ok("SUBMIT_JOB_LOW_BG", "fn-b", "b-low", b"x")
    client.submit_ok("SUBMIT_JOB_HIGH_BG", "fn-b", "b-high", b"x")
    assert _uniques(worker) == ["b-high", "a-1", "b-low"]


def test_one_worker_rotates_between_its_functions(server):
    client, worker = server.conn(), server.conn()
    worker.can_do("rot-x", "rot-y", "rot-z")
    for function, unique in (("rot-x", "x-1"), ("rot-x", "x-2"), ("rot-x", "x-3"), ("rot-y", "y-1"),
                             ("rot-z", "z-1"), ("rot-z", "z-2")):
        client.submit_ok("SUBMIT_JOB_BG", function, unique, b"x")
    assert _uniques(worker) == ["z-1", "y-1", "x-1", "z-2", "x-2", "x-3"]
    # The same connection keeps its rotation state; priorities still come first.
    for command, function, unique in (("SUBMIT_JOB_LOW_BG", "rot-x", "x-4"), ("SUBMIT_JOB_BG", "rot-y", "y-2"),
                                      ("SUBMIT_JOB_HIGH_BG", "rot-z", "z-3"), ("SUBMIT_JOB_HIGH_BG", "rot-x", "x-5"),
                                      ("SUBMIT_JOB_BG", "rot-y", "y-3"), ("SUBMIT_JOB_BG", "rot-z", "z-4")):
        client.submit_ok(command, function, unique, b"x")
    assert _uniques(worker) == ["z-3", "x-5", "y-2", "z-4", "y-3", "x-4"]


def test_rotation_is_independent_of_submission_order(server):
    client, first, second = server.conn(), server.conn(), server.conn()
    first.can_do("ord-b", "ord-a")
    second.can_do("ord-d", "ord-c")
    for function, unique in (("ord-a", "a-1"), ("ord-a", "a-2"), ("ord-b", "b-1"), ("ord-b", "b-2"),
                             ("ord-d", "d-1"), ("ord-d", "d-2"), ("ord-c", "c-1"), ("ord-c", "c-2")):
        client.submit_ok("SUBMIT_JOB_BG", function, unique, b"x")
    assert _uniques(first) == ["a-1", "b-1", "a-2", "b-2"]
    assert _uniques(second) == ["c-1", "d-1", "c-2", "d-2"]
    # Interleaving grabs with new submissions.
    worker = server.conn()
    worker.can_do("int-p", "int-q")
    for function, unique in (("int-p", "p-1"), ("int-q", "q-1"), ("int-p", "p-2")):
        client.submit_ok("SUBMIT_JOB_BG", function, unique, b"x")
    job = worker.grab()
    assert job[2] == b"q-1"
    worker.send("WORK_COMPLETE", job[0], b"")
    client.submit_ok("SUBMIT_JOB_BG", "int-q", "q-2", b"x")
    assert _uniques(worker) == ["p-1", "q-2", "p-2"]


def test_abandoned_foreground_job_is_not_delivered(server):
    client, worker = server.conn(), server.conn()
    gone = server.conn()
    gone.submit_ok("SUBMIT_JOB_HIGH", "abandon", "fg", b"nobody waits")
    client.submit_ok("SUBMIT_JOB_LOW_BG", "abandon", "bg", b"still wanted")
    gone.close()
    sync(client)
    time.sleep(0.5)
    worker.can_do("abandon")
    assert _drain(worker) == [(b"bg", b"still wanted")]


def test_many_jobs_spread_over_workers_run_once(server):
    # Adapted from the upstream lp_734663 regression: many background jobs, several workers.
    client = server.conn()
    handles = [client.submit_ok("SUBMIT_JOB_BG", "bulk", f"m-{i:02d}", f"d{i}".encode()) for i in range(40)]
    assert len(set(handles)) == 40
    assert len(server.rows()) == 40
    workers = [server.conn() for _ in range(4)]
    for worker in workers:
        worker.can_do("bulk")
    seen = []
    active = list(workers)
    while active:
        for worker in list(active):
            job = worker.grab()
            if job is None:
                active.remove(worker)
                continue
            seen.append((job[0], job[2], job[3]))
            worker.send("WORK_COMPLETE", job[0], b"")
    assert sorted(seen) == sorted((h, f"m-{i:02d}".encode(), f"d{i}".encode()) for i, h in enumerate(handles))
    for worker in workers:
        sync(worker)
    assert server.rows() == []
