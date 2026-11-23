"""Durability across server restarts: replay of stored rows into the in-memory queues."""

import time

import broker as support
from broker import is_error, row, sync


def _grab_all(worker, complete=True):
    jobs = []
    while (job := worker.grab()) is not None:
        jobs.append(job)
        if complete:
            worker.send("WORK_COMPLETE", job[0], b"")
    if complete:
        sync(worker)
    return jobs


def test_restart_replays_pending_background_jobs(server):
    client = server.conn()
    far = int(time.time()) + 86400
    client.submit_ok("SUBMIT_JOB_LOW_BG", "replay", "low", b"L")
    client.submit_ok("SUBMIT_JOB_BG", "replay", "normal", b"N")
    client.submit_ok("SUBMIT_JOB_HIGH_BG", "replay", "high", b"H\0\xff")
    client.submit_ok("SUBMIT_JOB_EPOCH", "replay", "far", b"F", epoch=far)
    client.submit_ok("SUBMIT_JOB_EPOCH", "replay", "past", b"P", epoch=1000)
    client.submit_ok("SUBMIT_JOB_BG", "replay", "done", b"D")
    client.submit_ok("SUBMIT_JOB", "replay", "foreground", b"not stored")
    worker = server.conn()
    worker.can_do("replay")
    done = worker.grab()
    assert done[2] == b"high"
    worker.send("WORK_COMPLETE", done[0], b"")
    sync(worker)
    stored = [row("low", "replay", 2, b"L"), row("normal", "replay", 1, b"N"),
              row("far", "replay", 1, b"F", far), row("past", "replay", 1, b"P", 1000), row("done", "replay", 1, b"D")]
    server.expect_rows(*stored)

    for _ in range(2):  # replaying neither duplicates rows nor loses them
        server.restart()
        server.expect_rows(*stored)
        assert server.admin().status() == [("replay", 5, 0, 0)]

    worker = server.conn()
    worker.can_do("replay")
    jobs = _grab_all(worker)
    # Normal-priority jobs come before the low one; order within a level after a restart is not graded.
    assert len(jobs) == 4 and {j[1] for j in jobs} == {b"replay"}
    assert sorted((j[2], j[3]) for j in jobs[:3]) == [(b"done", b"D"), (b"normal", b"N"), (b"past", b"P")]
    assert (jobs[3][2], jobs[3][3]) == (b"low", b"L")
    server.expect_rows(row("far", "replay", 1, b"F", far))
    assert server.admin().status() == [("replay", 1, 0, 1)]


def test_job_running_at_shutdown_is_replayed(server):
    client, worker = server.conn(), server.conn()
    handle = client.submit_ok("SUBMIT_JOB_BG", "inflight", "busy", b"half done")
    worker.can_do("inflight")
    assert worker.grab()[0] == handle
    worker.send("WORK_STATUS", handle, b"1", b"2")
    sync(worker)
    server.restart()
    server.expect_rows(row("busy", "inflight", 1, b"half done"))
    client = server.conn()
    worker = server.conn()
    worker.can_do("inflight")
    job = worker.grab()
    assert job[1:] == [b"inflight", b"busy", b"half done"]
    assert client.status(job[0]) == [job[0], b"1", b"1", b"0", b"0"]
    worker.send("WORK_COMPLETE", job[0], b"")
    sync(worker)
    server.expect_rows()


def test_externally_written_rows_are_replayed(mysql, make_broker, table):
    support.query(
        mysql,
        f"CREATE TABLE `{table}` (id INT AUTO_INCREMENT PRIMARY KEY, when_to_run INT, data LONGBLOB, priority INT, "
        "function_name VARCHAR(255), unique_key VARCHAR(64), UNIQUE KEY (unique_key, function_name))",
    )
    external = [
        ("n-1", "ext", 1, b"normal", 0), ("l-1", "ext", 2, b"low\0bin", 0), ("h-1", "ext", 0, b"high", 0),
        ("future", "ext", 0, b"not yet", 2000000000), ("old", "ext", 1, b"past epoch", 1000),
        ("", "ext", 1, b"anonymous", 0), ("o-1", "ext-other", 2, b"other function", 0),
    ]
    for values in external:
        support.query(mysql, f"INSERT INTO `{table}` (unique_key, function_name, priority, data, when_to_run) "
                             "VALUES (%s, %s, %s, %s, %s)", *values)
    server = make_broker(table)
    admin = server.admin()
    assert admin.status() == [("ext", 6, 0, 0), ("ext-other", 1, 0, 0)]
    server.expect_rows(*external)
    client = server.conn()
    # Replayed jobs coalesce with new submissions like any queued job.
    duplicate = client.submit_ok("SUBMIT_JOB_HIGH_BG", "ext", "l-1", b"replacement")
    response = client.submit("SUBMIT_JOB_BG", "ext", "", b"second anonymous")
    assert is_error(response, b"QUEUE_ERROR"), response
    server.expect_rows(*external)
    worker = server.conn()
    worker.can_do("ext")
    jobs = _grab_all(worker)
    assert [j[2] for j in jobs][0] == b"h-1"
    assert sorted(j[2] for j in jobs[1:4]) == [b"", b"n-1", b"old"]
    assert jobs[4][2] == b"l-1" and jobs[4][0] == duplicate and len(jobs) == 5
    assert {j[2]: j[3] for j in jobs} == {b"h-1": b"high", b"n-1": b"normal", b"old": b"past epoch",
                                          b"": b"anonymous", b"l-1": b"low\0bin"}
    server.expect_rows(("future", "ext", 0, b"not yet", 2000000000), ("o-1", "ext-other", 2, b"other function", 0))


def test_replayed_future_epoch_job_runs_on_time(mysql, make_broker, table):
    server = make_broker(table)
    client = server.conn()
    due = int(time.time()) + 7
    client.submit_ok("SUBMIT_JOB_EPOCH", "soon", "tick", b"T", epoch=due)
    client.submit_ok("SUBMIT_JOB_LOW_BG", "soon", "plain", b"P")
    server.restart()
    server.expect_rows(row("tick", "soon", 1, b"T", due), row("plain", "soon", 2, b"P"))
    worker = server.conn()
    worker.can_do("soon")
    assert [j[2] for j in _grab_all(worker)] == [b"plain"]
    assert time.time() < due, "test machine too slow for the scheduling window"
    while True:
        worker.send("PRE_SLEEP")
        assert worker.recv(timeout=due + 4 - time.time()) == ("NOOP", b"")
        job = worker.grab()
        if job is not None:
            break
    assert due - 1 <= time.time() <= due + 3
    assert job[1:] == [b"soon", b"tick", b"T"]
    worker.send("WORK_COMPLETE", job[0], b"")
    sync(worker)
    server.expect_rows()
