"""Session pool mode: a client keeps one server connection from its first query until it disconnects."""

import time

import psycopg
import pytest

from conftest import run_pooler, show_rows, tenant_pools, wait_until

pytestmark = pytest.mark.usefixtures("tenants")


def session_pooler(tenants, **general):
    pools = tenant_pools(tenants, alpha_settings={"pool_mode": "session"})
    return run_pooler(pools=pools, general=general)


def owner_rows(tenants):
    return tenants.activity(tenants.alpha.owner)


def test_session_keeps_one_server_connection_between_statements(tenants):
    with session_pooler(tenants) as pooler:
        with pooler.alpha() as conn, pooler.alpha() as other:
            pids = {conn.execute("SELECT pg_backend_pid()").fetchone()[0] for _ in range(6)}
            assert len(pids) == 1
            assert other.execute("SELECT pg_backend_pid()").fetchone()[0] not in pids


def test_session_settings_persist_between_statements(tenants):
    with session_pooler(tenants) as pooler:
        with pooler.alpha() as conn:
            conn.execute("SET statement_timeout = 4321")
            conn.execute("PREPARE twice(int) AS SELECT $1 * 2")
            assert conn.execute("SHOW statement_timeout").fetchall() == [("4321ms",)]
            assert conn.execute("EXECUTE twice(21)").fetchall() == [(42,)]


def test_pool_size_caps_concurrent_sessions(tenants):
    with session_pooler(tenants) as pooler:
        first, second, third = pooler.alpha(), pooler.alpha(), pooler.alpha()
        try:
            first.execute("SELECT 1")
            second.execute("SELECT 1")
            started = time.monotonic()
            with pytest.raises(psycopg.Error) as failed:
                third.execute("SELECT 1")
            assert (failed.value.sqlstate, failed.value.diag.severity_nonlocalized) == ("58000", "FATAL")
            assert 0.7 <= time.monotonic() - started <= 5
            assert owner_rows(tenants) == [(tenants.alpha.owner, tenants.alpha.database, "idle")] * 2
            assert not third.closed
        finally:
            for conn in (first, second, third):
                conn.close()


def test_ended_session_returns_its_server_to_the_pool(tenants):
    with session_pooler(tenants) as pooler:
        first, second, third = pooler.alpha(), pooler.alpha(), pooler.alpha()
        try:
            pid = first.execute("SELECT pg_backend_pid()").fetchone()[0]
            second.execute("SELECT 1")
            first.close()
            assert third.execute("SELECT pg_backend_pid()").fetchone()[0] == pid
        finally:
            for conn in (second, third):
                conn.close()


def test_session_state_is_reset_for_the_next_client(tenants):
    with session_pooler(tenants) as pooler:
        first, second = pooler.alpha(), pooler.alpha()
        first.execute("SET search_path = pg_catalog")
        first.execute("PREPARE leftover AS SELECT 1")
        second.execute("SELECT 1")
        first.close()
        with pooler.alpha() as third:
            assert third.execute("SHOW search_path").fetchall() == [('"$user", public',)]
            with pytest.raises(psycopg.Error) as failed:
                third.execute("EXECUTE leftover")
            assert failed.value.sqlstate == "26000"
        second.close()


def test_session_client_leaving_mid_transaction_rolls_back(tenants):
    with session_pooler(tenants) as pooler:
        conn = pooler.alpha()
        conn.execute("BEGIN")
        conn.execute("INSERT INTO ledger VALUES ('abandoned', 1)")
        conn.close()
        assert wait_until(lambda: all(row[2] == "idle" for row in owner_rows(tenants)), timeout=5)
        assert tenants.ledger(tenants.alpha) == []


def test_show_users_reports_each_pool_mode(tenants):
    with session_pooler(tenants) as pooler:
        with pooler.admin() as admin:
            assert show_rows(admin, "SHOW USERS")[1] == [("app", "session"), ("app", "transaction")]
