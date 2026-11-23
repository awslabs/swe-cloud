"""Text admin protocol: status and workers listings."""

import time

from broker import sync, wait_for


def test_status_counts_queued_running_and_workers(server):
    client, admin = server.conn(), server.admin()
    assert admin.status() == []
    client.submit_ok("SUBMIT_JOB_BG", "count-a", "a-1", b"1")
    client.submit_ok("SUBMIT_JOB_LOW_BG", "count-a", "a-2", b"2")
    client.submit_ok("SUBMIT_JOB_EPOCH", "count-a", "a-3", b"3", epoch=int(time.time()) + 3600)
    client.submit_ok("SUBMIT_JOB_HIGH", "count-b", "b-1", b"4")
    busy, idle, other = server.conn(), server.conn(), server.conn()
    busy.can_do("count-a")
    idle.can_do("count-a", "count-c")
    other.can_do("count-c")
    assert busy.grab()[2] == b"a-1"
    for conn in (busy, idle, other):
        sync(conn)
    assert admin.status() == [("count-a", 3, 1, 2), ("count-b", 1, 0, 0), ("count-c", 0, 0, 2)]
    # Several commands on one admin connection.
    assert admin.status() == [("count-a", 3, 1, 2), ("count-b", 1, 0, 0), ("count-c", 0, 0, 2)]


def test_status_tracks_completion_and_departures(server):
    client, admin, worker = server.conn(), server.admin(), server.conn()
    client.submit_ok("SUBMIT_JOB_BG", "track", "t-1", b"1")
    worker.can_do("track", "gone")
    job = worker.grab()
    worker.send("WORK_COMPLETE", job[0], b"")
    worker.send("CANT_DO", "gone")
    sync(worker)
    # Known functions stay listed with zero counts.
    assert admin.status() == [("gone", 0, 0, 0), ("track", 0, 0, 1)]
    worker.close()
    assert wait_for(lambda: admin.status() == [("gone", 0, 0, 0), ("track", 0, 0, 0)])


def test_workers_lists_every_connection(server):
    admin = server.admin()
    client = server.conn()
    named, anonymous = server.conn(), server.conn()
    named.send("SET_CLIENT_ID", "worker-one")
    named.can_do("w-x", "w-y", "w-z")
    named.send("CANT_DO", "w-y")
    anonymous.can_do("w-z")
    client.submit_ok("SUBMIT_JOB_BG", "w-x", "k", b"")
    for conn in (named, anonymous):
        sync(conn)
    expected = sorted([("127.0.0.1", "-", ()), ("127.0.0.1", "-", ()),
                       ("127.0.0.1", "worker-one", ("w-x", "w-z")), ("127.0.0.1", "-", ("w-z",))])
    assert wait_for(lambda: admin.workers() == expected), admin.workers()
    named.send("RESET_ABILITIES")
    anonymous.close()
    sync(named)
    expected = sorted([("127.0.0.1", "-", ()), ("127.0.0.1", "-", ()), ("127.0.0.1", "worker-one", ())])
    assert wait_for(lambda: admin.workers() == expected), admin.workers()
