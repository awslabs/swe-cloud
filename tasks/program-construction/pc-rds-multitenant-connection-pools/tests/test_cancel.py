"""Query cancellation: a CancelRequest carrying a client's BackendKeyData reaches that client's server query."""

import socket
import struct
import threading
import time

import psycopg
import pytest

pytestmark = pytest.mark.usefixtures("tenants")


def _sleep_in_thread(conn, seconds):
    result = {}

    def run():
        started = time.monotonic()
        try:
            result["rows"] = conn.execute(f"SELECT 'done' FROM pg_sleep({seconds})").fetchall()
        except psycopg.Error as exc:
            result["error"] = (exc.sqlstate, exc.diag.severity_nonlocalized)
        result["elapsed"] = time.monotonic() - started

    thread = threading.Thread(target=run)
    thread.start()
    return thread, result


def _send_cancel(port, pid, key):
    with socket.create_connection(("127.0.0.1", port), 5) as sock:
        sock.sendall(struct.pack("!iiii", 16, 80877102, pid, key))
        sock.settimeout(5)
        try:
            while sock.recv(64):
                pass
        except OSError:
            pass


def test_cancel_interrupts_the_running_query(tenants, pooler):
    with pooler.alpha() as conn:
        thread, result = _sleep_in_thread(conn, 20)
        time.sleep(1.5)
        conn.cancel()
        thread.join(15)
        assert result["error"] == ("57014", "ERROR")
        assert result["elapsed"] < 10
        assert conn.execute("SELECT 'after cancel'").fetchall() == [("after cancel",)]


def test_cancel_with_a_wrong_key_is_ignored(tenants, pooler):
    with pooler.alpha() as conn:
        thread, result = _sleep_in_thread(conn, 3)
        time.sleep(1)
        _send_cancel(pooler.port, conn.pgconn.backend_pid, 12345)
        _send_cancel(pooler.port, conn.pgconn.backend_pid ^ 0x5A5A, 0)
        thread.join(15)
        assert result.get("rows") == [("done",)]


def test_cancel_reaches_only_its_own_client(tenants, pooler):
    with pooler.alpha() as target, pooler.alpha() as bystander:
        target_thread, target_result = _sleep_in_thread(target, 20)
        bystander_thread, bystander_result = _sleep_in_thread(bystander, 4)
        time.sleep(1.5)
        target.cancel()
        target_thread.join(15)
        bystander_thread.join(15)
        assert target_result["error"] == ("57014", "ERROR")
        assert bystander_result.get("rows") == [("done",)]


def test_cancel_of_an_idle_client_leaves_it_usable(tenants, pooler):
    with pooler.alpha() as conn:
        conn.execute("SELECT 1")
        conn.cancel()
        time.sleep(0.5)
        assert conn.execute("SELECT 'still fine'").fetchall() == [("still fine",)]
