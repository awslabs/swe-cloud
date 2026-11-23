"""Packet-level behaviour: handles, assignments, status queries, result forwarding and error replies."""

import broker as support
from broker import is_error, row, sync

BINARY = b"\0head\xff\xfe\0\0tail\n"


def test_echo_and_pipelined_requests(server):
    conn = server.conn()
    # Several packets in one write are answered in order.
    body = b""
    for payload in (b"one", b"", BINARY):
        body += b"\0REQ" + (16).to_bytes(4, "big") + len(payload).to_bytes(4, "big") + payload
    conn.sock.sendall(body)
    assert [conn.recv() for _ in range(3)] == [("ECHO_RES", b"one"), ("ECHO_RES", b""), ("ECHO_RES", BINARY)]


def test_handles_identify_jobs(server):
    conn = server.conn()
    first = conn.submit_ok("SUBMIT_JOB_BG", "alpha", "u-1", b"a")
    second = conn.submit_ok("SUBMIT_JOB_BG", "alpha", "u-2", b"b")
    other_function = conn.submit_ok("SUBMIT_JOB_BG", "beta", "u-1", b"c")
    assert len({first, second, other_function}) == 3
    # A second connection submitting the same unique id gets the queued job's handle.
    assert server.conn().submit_ok("SUBMIT_JOB_BG", "alpha", "u-1", b"z") == first


def test_assignment_packets(server):
    client, worker = server.conn(), server.conn()
    plain = client.submit_ok("SUBMIT_JOB_BG", "assign", "p-1", BINARY)
    uniq = client.submit_ok("SUBMIT_JOB_BG", "assign", "q-1", b"second\0job")
    worker.can_do("assign")
    assert worker.grab(unique=False) == [plain, b"assign", BINARY]
    assert worker.grab(unique=True) == [uniq, b"assign", b"q-1", b"second\0job"]
    assert worker.grab() is None


def test_get_status_through_job_lifecycle(server):
    client, worker = server.conn(), server.conn()
    handle = client.submit_ok("SUBMIT_JOB_BG", "progress", "g-1", b"x")
    assert client.status(handle) == [handle, b"1", b"0", b"0", b"0"]
    worker.can_do("progress")
    assert worker.grab()[0] == handle
    assert client.status(handle) == [handle, b"1", b"1", b"0", b"0"]
    worker.send("WORK_STATUS", handle, b"3", b"7")
    sync(worker)
    assert client.status(handle) == [handle, b"1", b"1", b"3", b"7"]
    worker.send("WORK_COMPLETE", handle, b"done")
    sync(worker)
    assert client.status(handle) == [handle, b"0", b"0", b"0", b"0"]
    assert client.status(b"H:no-such-job:42") == [b"H:no-such-job:42", b"0", b"0", b"0", b"0"]


def test_foreground_client_receives_progress_and_result(server):
    client, other, worker = server.conn(), server.conn(), server.conn()
    handle = client.submit_ok("SUBMIT_JOB", "fg", "f-1", b"work")
    # A second foreground submission of the same job joins it and also receives its packets.
    assert other.submit_ok("SUBMIT_JOB_LOW", "fg", "f-1", b"ignored") == handle
    assert client.status(handle) == [handle, b"1", b"0", b"0", b"0"]
    worker.can_do("fg")
    assert worker.grab() == [handle, b"fg", b"f-1", b"work"]
    worker.send("WORK_STATUS", handle, b"1", b"2")
    for conn in (client, other):
        assert conn.recv() == ("WORK_STATUS", handle + b"\0" + b"1\0" + b"2")
    worker.send("WORK_COMPLETE", handle, BINARY)
    for conn in (client, other):
        assert conn.recv() == ("WORK_COMPLETE", handle + b"\0" + BINARY)
    assert client.nothing_pending() and other.nothing_pending()
    assert server.rows() == []


def test_foreground_failure_is_forwarded(server):
    client, worker = server.conn(), server.conn()
    handle = client.submit_ok("SUBMIT_JOB_HIGH", "fails", "x-1", b"w")
    worker.can_do("fails")
    assert worker.grab()[0] == handle
    worker.send("WORK_FAIL", handle)
    assert client.recv() == ("WORK_FAIL", handle)
    assert client.status(handle) == [handle, b"0", b"0", b"0", b"0"]


def test_over_long_unique_is_rejected(server):
    conn = server.conn()
    keep = conn.submit_ok("SUBMIT_JOB_BG", "long", "short", b"kept")
    for size in (65, 100):
        response = conn.submit("SUBMIT_JOB_BG", "long", "u" * size, b"too long")
        assert is_error(response, b"ARGUMENT_TOO_LARGE"), response
    sync(conn)
    assert conn.submit_ok("SUBMIT_JOB_BG", "long", "q" * 63, b"max") not in (b"", keep)
    server.expect_rows(row("short", "long", 1, b"kept"), row("q" * 63, "long", 1, b"max"))


def test_results_for_jobs_not_held_are_errors(server):
    client, holder, other = server.conn(), server.conn(), server.conn()
    handle = client.submit_ok("SUBMIT_JOB_BG", "guarded", "g-1", b"data")
    other.can_do("guarded")
    # Not yet assigned to anyone.
    assert is_error(_result(other, "WORK_COMPLETE", handle, b"early"), b"JOB_NOT_FOUND")
    holder.can_do("guarded")
    assert holder.grab()[0] == handle
    for command, args in (("WORK_COMPLETE", (handle, b"stolen")), ("WORK_FAIL", (handle,)),
                          ("WORK_COMPLETE", (b"H:unknown:1", b"x")), ("WORK_FAIL", (b"H:unknown:1",))):
        assert is_error(_result(other, command, *args), b"JOB_NOT_FOUND")
    server.expect_rows(row("g-1", "guarded", 1, b"data"))
    assert client.status(handle) == [handle, b"1", b"1", b"0", b"0"]
    holder.send("WORK_COMPLETE", handle, b"ok")
    sync(holder)
    assert server.rows() == []


def _result(conn, command, *args):
    conn.send(command, *args)
    return conn.recv()


def test_grab_without_abilities_or_matching_jobs(server):
    client, worker = server.conn(), server.conn()
    assert worker.grab() is None
    client.submit_ok("SUBMIT_JOB_BG", "wanted", "w-1", b"x")
    worker.can_do("unrelated")
    assert worker.grab() is None
    assert support.rows(server.mysql, server.table) == [row("w-1", "wanted", 1, b"x")]
