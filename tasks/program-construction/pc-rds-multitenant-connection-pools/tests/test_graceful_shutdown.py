"""Graceful shutdown on SIGINT: new clients are refused, open transactions finish, shutdown_timeout bounds the wait."""

import signal
import time

import psycopg
import pytest

from conftest import run_pooler, tenant_pools
from pgwire import refusal

pytestmark = pytest.mark.usefixtures("tenants")


def test_sigint_without_clients_exits_cleanly(tenants):
    with run_pooler(pools=tenant_pools(tenants)) as pooler:
        pooler.process.send_signal(signal.SIGINT)
        assert pooler.process.wait(10) == 0


def test_sigint_refuses_new_clients(tenants):
    with run_pooler(pools=tenant_pools(tenants), general={"shutdown_timeout": 30000}) as pooler:
        holder = pooler.alpha()
        try:
            holder.execute("BEGIN")
            holder.execute("SELECT 1")
            pooler.process.send_signal(signal.SIGINT)
            time.sleep(1)
            assert pooler.process.poll() is None
            assert refusal(pooler.port, "beta", "app", "beta-secret")[:2] == ([], [("FATAL", "58000")])
        finally:
            holder.close()


def test_sigint_lets_an_open_transaction_commit(tenants):
    with run_pooler(pools=tenant_pools(tenants), general={"shutdown_timeout": 30000}) as pooler:
        conn = pooler.alpha()
        conn.execute("BEGIN")
        conn.execute("INSERT INTO ledger VALUES ('draining', 1)")
        pooler.process.send_signal(signal.SIGINT)
        time.sleep(1)
        assert conn.execute("SELECT count(*) FROM ledger").fetchall() == [(1,)]
        conn.execute("COMMIT")
        # Once its transaction is over the client is let go and the program exits.
        with pytest.raises(psycopg.OperationalError):
            conn.execute("SELECT 1")
        conn.close()
        assert pooler.process.wait(10) == 0
    assert tenants.ledger(tenants.alpha) == [("draining", 1)]


def test_sigint_closes_idle_clients(tenants):
    with run_pooler(pools=tenant_pools(tenants), general={"shutdown_timeout": 30000}) as pooler:
        conn = pooler.alpha()
        conn.execute("SELECT 1")
        pooler.process.send_signal(signal.SIGINT)
        assert pooler.process.wait(10) == 0
        with pytest.raises(psycopg.OperationalError):
            conn.execute("SELECT 1")
        conn.close()


def test_shutdown_timeout_bounds_the_wait_for_open_transactions(tenants):
    with run_pooler(pools=tenant_pools(tenants), general={"shutdown_timeout": 2000}) as pooler:
        conn = pooler.alpha()
        try:
            conn.execute("BEGIN")
            conn.execute("SELECT 1")
            started = time.monotonic()
            pooler.process.send_signal(signal.SIGINT)
            assert pooler.process.wait(15) == 0
            assert 1.5 <= time.monotonic() - started <= 8
        finally:
            conn.close()
