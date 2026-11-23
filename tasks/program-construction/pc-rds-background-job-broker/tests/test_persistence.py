"""How individual submissions and work results change the queue table."""

import time

from broker import is_error, row, sync

BINARY = bytes(range(256)) + b"\0\0"


def test_background_rows_record_priority_and_workload(server):
    conn = server.conn()
    conn.submit_ok("SUBMIT_JOB_HIGH_BG", "persist", "high", b"h\0\xffdata")
    conn.submit_ok("SUBMIT_JOB_BG", "persist", "normal", b"normal")
    conn.submit_ok("SUBMIT_JOB_LOW_BG", "persist", "low", BINARY)
    conn.submit_ok("SUBMIT_JOB_BG", "persist", "spaces and ünïcode", b"text")
    server.expect_rows(
        row("high", "persist", 0, b"h\0\xffdata"),
        row("normal", "persist", 1, b"normal"),
        row("low", "persist", 2, BINARY),
        row("spaces and ünïcode", "persist", 1, b"text"),
    )


def test_epoch_rows_record_run_time(server):
    conn = server.conn()
    future = int(time.time()) + 86400
    conn.submit_ok("SUBMIT_JOB_EPOCH", "timed", "later", b"tomorrow", epoch=future)
    conn.submit_ok("SUBMIT_JOB_EPOCH", "timed", "past", b"long ago", epoch=1000)
    conn.submit_ok("SUBMIT_JOB_EPOCH", "timed", "max", b"edge", epoch=2147483647)
    server.expect_rows(
        row("later", "timed", 1, b"tomorrow", future),
        row("past", "timed", 1, b"long ago", 1000),
        row("max", "timed", 1, b"edge", 2147483647),
    )


def test_large_and_empty_workloads(server):
    conn, worker = server.conn(), server.conn()
    big = bytes(i % 251 for i in range(1_000_000))
    conn.submit_ok("SUBMIT_JOB_BG", "sizes", "big", big)
    conn.submit_ok("SUBMIT_JOB_BG", "sizes", "empty", b"")
    server.expect_rows(row("big", "sizes", 1, big), row("empty", "sizes", 1, b""))
    worker.can_do("sizes")
    assert worker.grab()[2:] == [b"big", big]
    assert worker.grab()[2:] == [b"empty", b""]


def test_foreground_jobs_are_never_stored(server):
    bg, fg, worker = server.conn(), server.conn(), server.conn()
    bg.submit_ok("SUBMIT_JOB_BG", "mixed", "sentinel", b"bg")
    sentinel = row("sentinel", "mixed", 1, b"bg")
    handles = [fg.submit_ok(command, "mixed", f"fg-{i}", b"fg") for i, command in
               enumerate(("SUBMIT_JOB", "SUBMIT_JOB_HIGH", "SUBMIT_JOB_LOW"))]
    server.expect_rows(sentinel)
    worker.can_do("mixed")
    job = worker.grab()
    assert job[0] == handles[1]  # the high-priority foreground job runs first
    server.expect_rows(sentinel)
    worker.send("WORK_COMPLETE", job[0], b"r")
    assert fg.recv() == ("WORK_COMPLETE", job[0] + b"\0r")
    server.expect_rows(sentinel)


def test_duplicate_submission_keeps_original_row(server):
    conn, admin = server.conn(), server.admin()
    handle = conn.submit_ok("SUBMIT_JOB_LOW_BG", "dups", "same", b"original")
    for command in ("SUBMIT_JOB_BG", "SUBMIT_JOB_HIGH_BG", "SUBMIT_JOB_LOW_BG"):
        assert conn.submit_ok(command, "dups", "same", b"replacement") == handle
    assert conn.submit_ok("SUBMIT_JOB_EPOCH", "dups", "same", b"epoch", epoch=int(time.time()) + 3600) == handle
    server.expect_rows(row("same", "dups", 2, b"original"))
    assert admin.status() == [("dups", 1, 0, 0)]
    # The same unique id under another function is an independent job.
    other = conn.submit_ok("SUBMIT_JOB_BG", "dups-other", "same", b"independent")
    assert other != handle
    server.expect_rows(row("same", "dups", 2, b"original"), row("same", "dups-other", 1, b"independent"))


def test_complete_and_fail_remove_only_their_rows(server):
    conn, worker = server.conn(), server.conn()
    conn.submit_ok("SUBMIT_JOB_BG", "work", "ok", b"1")
    conn.submit_ok("SUBMIT_JOB_BG", "work", "bad", b"2")
    conn.submit_ok("SUBMIT_JOB_BG", "elsewhere", "ok", b"3")
    sentinel = row("ok", "elsewhere", 1, b"3")
    worker.can_do("work")
    first, second = worker.grab(), worker.grab()
    assert [first[2], second[2]] == [b"ok", b"bad"]
    server.expect_rows(row("ok", "work", 1, b"1"), row("bad", "work", 1, b"2"), sentinel)
    worker.send("WORK_COMPLETE", first[0], b"result")
    sync(worker)
    server.expect_rows(row("bad", "work", 1, b"2"), sentinel)
    worker.send("WORK_FAIL", second[0])
    sync(worker)
    server.expect_rows(sentinel)
    assert conn.status(second[0]) == [second[0], b"0", b"0", b"0", b"0"]


def test_unique_id_reusable_after_completion(server):
    conn, worker = server.conn(), server.conn()
    first = conn.submit_ok("SUBMIT_JOB_BG", "again", "k", b"first")
    worker.can_do("again")
    assert worker.grab()[0] == first
    # Still the same job while it runs: no new row, the original workload stays.
    assert conn.submit_ok("SUBMIT_JOB_HIGH_BG", "again", "k", b"while running") == first
    server.expect_rows(row("k", "again", 1, b"first"))
    worker.send("WORK_COMPLETE", first, b"")
    sync(worker)
    second = conn.submit_ok("SUBMIT_JOB_HIGH_BG", "again", "k", b"second")
    assert second != first
    server.expect_rows(row("k", "again", 0, b"second"))
    assert worker.grab() == [second, b"again", b"k", b"second"]


def test_worker_disconnect_requeues_job(server):
    client, holder, sleeper = server.conn(), server.conn(), server.conn()
    handle = client.submit_ok("SUBMIT_JOB_BG", "fragile", "r-1", b"retry me")
    holder.can_do("fragile")
    sleeper.can_do("fragile")
    assert holder.grab()[0] == handle
    assert sleeper.grab() is None
    sleeper.send("PRE_SLEEP")
    sync(sleeper)
    holder.close()
    assert sleeper.recv() == ("NOOP", b"")
    server.expect_rows(row("r-1", "fragile", 1, b"retry me"))
    assert client.status(handle) == [handle, b"1", b"0", b"0", b"0"]
    assert sleeper.grab() == [handle, b"fragile", b"r-1", b"retry me"]
    sleeper.send("WORK_COMPLETE", handle, b"")
    sync(sleeper)
    assert server.rows() == []


def test_empty_unique_ids_are_stored_literally(server):
    conn, admin, worker = server.conn(), server.admin(), server.conn()
    first = conn.submit_ok("SUBMIT_JOB_BG", "anon", "", b"first")
    server.expect_rows(row("", "anon", 1, b"first"))
    # Another stored job with an empty unique id would collide with that row.
    for command in ("SUBMIT_JOB_BG", "SUBMIT_JOB_HIGH_BG"):
        response = conn.submit(command, "anon", "", b"second")
        assert is_error(response, b"QUEUE_ERROR"), response
    server.expect_rows(row("", "anon", 1, b"first"))
    assert admin.status() == [("anon", 1, 0, 0)]
    # Foreground jobs with an empty unique id are separate jobs.
    fg_a = conn.submit_ok("SUBMIT_JOB", "anon", "", b"fg-a")
    fg_b = server.conn().submit_ok("SUBMIT_JOB", "anon", "", b"fg-b")
    assert len({first, fg_a, fg_b}) == 3
    worker.can_do("anon")
    assert [worker.grab() for _ in range(3)] == [[first, b"anon", b"", b"first"], [fg_a, b"anon", b"", b"fg-a"],
                                                 [fg_b, b"anon", b"", b"fg-b"]]
    worker.send("WORK_COMPLETE", first, b"")
    sync(worker)
    server.expect_rows()
    conn.submit_ok("SUBMIT_JOB_LOW_BG", "anon", "", b"third")
    server.expect_rows(row("", "anon", 2, b"third"))


def test_dash_unique_matches_by_workload(server):
    conn = server.conn()
    handle = conn.submit_ok("SUBMIT_JOB_BG", "dash", "-", b"payload")
    assert conn.submit_ok("SUBMIT_JOB_LOW_BG", "dash", "-", b"payload") == handle
    server.expect_rows(row("-", "dash", 1, b"payload"))
    response = conn.submit("SUBMIT_JOB_BG", "dash", "-", b"different payload")
    assert is_error(response, b"QUEUE_ERROR"), response
    server.expect_rows(row("-", "dash", 1, b"payload"))
    worker = server.conn()
    worker.can_do("dash")
    assert worker.grab() == [handle, b"dash", b"-", b"payload"]
    assert worker.grab() is None


def test_background_submission_joins_queued_foreground_job(server):
    fg, bg, worker = server.conn(), server.conn(), server.conn()
    handle = fg.submit_ok("SUBMIT_JOB", "join", "shared", b"fg-data")
    assert bg.submit_ok("SUBMIT_JOB_BG", "join", "shared", b"bg-data") == handle
    server.expect_rows()
    worker.can_do("join")
    assert worker.grab() == [handle, b"join", b"shared", b"fg-data"]
    worker.send("WORK_COMPLETE", handle, b"answer")
    assert fg.recv() == ("WORK_COMPLETE", handle + b"\0answer")
    server.expect_rows()
    assert worker.grab() is None


def test_foreground_submission_joins_stored_background_job(server):
    bg, fg, worker = server.conn(), server.conn(), server.conn()
    handle = bg.submit_ok("SUBMIT_JOB_LOW_BG", "join", "shared", b"bg-data")
    assert fg.submit_ok("SUBMIT_JOB_HIGH", "join", "shared", b"fg-data") == handle
    server.expect_rows(row("shared", "join", 2, b"bg-data"))
    worker.can_do("join")
    assert worker.grab() == [handle, b"join", b"shared", b"bg-data"]
    worker.send("WORK_STATUS", handle, b"5", b"10")
    assert fg.recv() == ("WORK_STATUS", handle + b"\x005\x0010")
    worker.send("WORK_COMPLETE", handle, b"answer")
    assert fg.recv() == ("WORK_COMPLETE", handle + b"\0answer")
    sync(worker)
    server.expect_rows()
    assert bg.nothing_pending()
