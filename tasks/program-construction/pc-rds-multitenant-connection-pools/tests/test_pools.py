"""Transaction pooling: server connection reuse, per-pool budgets, checkout timeout and return on disconnect."""

import os
import socket
import threading
import time

import psycopg
import pytest

from conftest import run_pooler, tenant_pools, wait_until
from pgwire import refusal


def owner_rows(tenants, tenant):
    return [row for row in tenants.activity() if row[0] == tenant.owner]


def hold(conn, key):
    conn.execute("BEGIN")
    conn.execute(f"INSERT INTO ledger VALUES ('{key}', 1)")


def test_sequential_clients_reuse_server_connections(tenants, pooler):
    pids = []
    for _ in range(8):
        with pooler.alpha() as conn:
            pids.append(conn.execute("SELECT pg_backend_pid()").fetchone()[0])
    assert 1 <= len(set(pids)) <= 2
    activity = owner_rows(tenants, tenants.alpha)
    assert 1 <= len(activity) <= 2
    assert all(row == (tenants.alpha.owner, tenants.alpha.database, "idle") for row in activity)


def test_idle_clients_beyond_pool_size_share_the_servers(tenants, pooler):
    clients = [pooler.alpha() for _ in range(6)]
    try:
        pids = []
        for _ in range(2):
            for index, conn in enumerate(clients):
                row = conn.execute(f"SELECT pg_backend_pid(), {index}").fetchone()
                assert row[1] == index
                pids.append(row[0])
        assert len(pids) == 12
        assert 1 <= len(set(pids)) <= 2
        assert 1 <= len(owner_rows(tenants, tenants.alpha)) <= 2
    finally:
        for conn in clients:
            conn.close()


def test_transaction_keeps_one_server_connection(tenants, pooler):
    with pooler.alpha() as a, pooler.alpha() as other:
        a.execute("BEGIN")
        pid = a.execute("SELECT pg_backend_pid()").fetchone()[0]
        a.execute("INSERT INTO ledger VALUES ('t', 1)")
        # Another client is served by the remaining server connection meanwhile.
        assert other.execute("SELECT pg_backend_pid()").fetchone()[0] != pid
        assert other.execute("SELECT count(*) FROM ledger").fetchall() == [(0,)]
        assert a.execute("SELECT pg_backend_pid(), count(*) FROM ledger").fetchall() == [(pid, 1)]
        a.execute("COMMIT")
    assert tenants.ledger(tenants.alpha) == [("t", 1)]


def test_full_pool_holds_exactly_pool_size_servers(tenants, pooler):
    holders = [pooler.beta() for _ in range(3)]
    try:
        for index, conn in enumerate(holders):
            hold(conn, f"h{index}")
        assert wait_until(lambda: owner_rows(tenants, tenants.beta) == [
            (tenants.beta.owner, tenants.beta.database, "idle in transaction")] * 3)
        pids = {conn.execute("SELECT pg_backend_pid()").fetchone()[0] for conn in holders}
        assert len(pids) == 3
    finally:
        for conn in holders:
            conn.execute("ROLLBACK")
            conn.close()
    assert tenants.ledger(tenants.beta) == []


def test_checkout_timeout_reports_error_and_keeps_client_session(tenants, pooler):
    holders = [pooler.alpha() for _ in range(2)]
    waiter = pooler.alpha()
    try:
        for index, conn in enumerate(holders):
            hold(conn, f"h{index}")
        started = time.monotonic()
        with pytest.raises(psycopg.Error) as failed:
            waiter.execute("INSERT INTO ledger VALUES ('late', 1)")
        elapsed = time.monotonic() - started
        assert failed.value.sqlstate == "58000"
        assert failed.value.diag.severity_nonlocalized == "FATAL"
        assert 0.7 <= elapsed <= 5  # default checkout timeout: one second
        assert owner_rows(tenants, tenants.alpha) == [
            (tenants.alpha.owner, tenants.alpha.database, "idle in transaction")] * 2
        assert not waiter.closed
        holders[0].execute("COMMIT")
        # The same client session continues once a server connection is free.
        assert waiter.execute("SELECT 'again', current_user::text").fetchall() == [("again", tenants.alpha.owner)]
        assert waiter.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    finally:
        for conn in holders + [waiter]:
            conn.close()
    assert tenants.ledger(tenants.alpha) == [("h0", 1)]


def test_configured_checkout_timeout_lets_waiters_queue(tenants):
    with run_pooler(pools=tenant_pools(tenants), general={"connect_timeout": 8000}) as pooler:
        holders = [pooler.alpha() for _ in range(2)]
        result = {}

        def wait_for_server():
            started = time.monotonic()
            try:
                with pooler.alpha() as conn:
                    result["rows"] = conn.execute("SELECT 'served', count(*) FROM ledger").fetchall()
            except psycopg.Error as exc:
                result["error"] = exc.sqlstate
            result["elapsed"] = time.monotonic() - started

        try:
            for index, conn in enumerate(holders):
                hold(conn, f"h{index}")
            thread = threading.Thread(target=wait_for_server)
            thread.start()
            time.sleep(2)
            assert "rows" not in result and "error" not in result
            assert owner_rows(tenants, tenants.alpha) == [
                (tenants.alpha.owner, tenants.alpha.database, "idle in transaction")] * 2
            holders[0].execute("COMMIT")
            thread.join(10)
            assert result.get("rows") == [("served", 1)]
            assert 1.8 <= result["elapsed"] < 8
        finally:
            for conn in holders:
                conn.close()


def test_each_user_of_an_alias_has_its_own_pool(tenants):
    tenants.add_role("reporter", "reporter-pw", tenants.alpha)
    reporter = {"username": f"{tenants.prefix}_reporter", "password": "reporter-pw", "pool_size": 1}
    with run_pooler(pools=tenant_pools(tenants, alpha_users=[reporter])) as pooler:
        holders = [pooler.alpha() for _ in range(2)]
        try:
            for index, conn in enumerate(holders):
                hold(conn, f"h{index}")
            with pooler.connect("alpha", reporter["username"], "reporter-pw") as rep:
                # No server_username: the backend login is the frontend user and password.
                assert rep.execute("SELECT current_database()::text, current_user::text, (SELECT tenant FROM marker)"
                                   ).fetchall() == [(tenants.alpha.database, reporter["username"], "alpha")]
                rep.execute("BEGIN")
                rep.execute("SELECT 1")
                with pooler.connect("alpha", reporter["username"], "reporter-pw") as second:
                    with pytest.raises(psycopg.Error) as failed:
                        second.execute("SELECT 2")
                    assert failed.value.sqlstate == "58000"
                rep.execute("COMMIT")
            # Each user of the alias has its own password.
            assert refusal(pooler.port, "alpha", reporter["username"], "alpha-secret")[:2] == ([5], [("FATAL", "28P01")])
            assert refusal(pooler.port, "alpha", "app", "reporter-pw")[:2] == ([5], [("FATAL", "28P01")])
            activity = tenants.activity(tenants.alpha.owner, reporter["username"])
            assert activity.count((tenants.alpha.owner, tenants.alpha.database, "idle in transaction")) == 2
            assert [row for row in activity if row[0] == reporter["username"]] == [
                (reporter["username"], tenants.alpha.database, "idle")]
        finally:
            for conn in holders:
                conn.close()


@pytest.mark.parametrize("how", ["terminate", "socket"])
def test_client_leaving_mid_transaction_rolls_back_and_frees_server(tenants, pooler, how):
    conn = pooler.alpha()
    conn.execute("BEGIN")
    conn.execute("INSERT INTO ledger VALUES ('gone', 1)")
    pid = conn.execute("SELECT pg_backend_pid()").fetchone()[0]
    if how == "terminate":
        conn.close()
    else:
        socket.socket(fileno=os.dup(conn.pgconn.socket)).shutdown(socket.SHUT_RDWR)
    assert wait_until(lambda: all(row[2] == "idle" for row in owner_rows(tenants, tenants.alpha)), timeout=5)
    # The server connection itself survives: it was rolled back and returned to the pool, not closed.
    assert tenants.engines[0].query(f"SELECT state FROM pg_stat_activity WHERE pid = {pid}") == [("idle",)]
    assert tenants.ledger(tenants.alpha) == []
    # Both server connections are available again.
    holders = [pooler.alpha() for _ in range(2)]
    try:
        for index, holder in enumerate(holders):
            hold(holder, f"h{index}")
        assert owner_rows(tenants, tenants.alpha) == [
            (tenants.alpha.owner, tenants.alpha.database, "idle in transaction")] * 2
    finally:
        for holder in holders:
            holder.close()
    if how == "socket":
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def test_session_settings_outside_a_transaction_do_not_persist(tenants, pooler):
    with pooler.alpha() as conn, pooler.alpha() as other:
        default = other.execute("SHOW statement_timeout").fetchone()[0]
        assert default != "1234ms"
        conn.execute("SET statement_timeout = 1234")
        assert conn.execute("SHOW statement_timeout").fetchall() == [(default,)]
        assert other.execute("SHOW statement_timeout").fetchall() == [(default,)]
        cursor = conn.cursor()
        cursor.execute("SET statement_timeout = 2345; SHOW statement_timeout")
        assert cursor.nextset() and cursor.fetchall() == [("2345ms",)]
        assert conn.execute("SHOW statement_timeout").fetchall() == [(default,)]
        conn.execute("BEGIN")
        conn.execute("SET statement_timeout = 3456")
        assert conn.execute("SHOW statement_timeout").fetchall() == [("3456ms",)]
        assert other.execute("SHOW statement_timeout").fetchall() == [(default,)]
        conn.execute("ROLLBACK")
        conn.execute("SET search_path = pg_catalog")
        assert conn.execute("SHOW search_path").fetchall() == [('"$user", public',)]
