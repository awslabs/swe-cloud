"""Client and server connection lifetimes: idle-in-transaction and statement timeouts, idle and lifetime limits."""

import struct
import time

import pytest

from conftest import run_pooler, tenant_pools, wait_until
from pgwire import Wire, error_fields, md5_response

pytestmark = pytest.mark.usefixtures("tenants")


def alpha_rows(tenants):
    return tenants.activity(tenants.alpha.owner)


def _idle_in_transaction(wire):
    assert [kind for kind, _ in wire.query("BEGIN")] == [b"C", b"Z"]
    reply = wire.query("INSERT INTO ledger VALUES ('stalled', 1)")
    assert reply[-1] == (b"Z", b"T")


def test_idle_transaction_timeout_sends_an_error_without_a_query(tenants):
    with run_pooler(pools=tenant_pools(tenants), general={"idle_client_in_transaction_timeout": 1500}) as pooler:
        with _wire_session(pooler.port) as wire:
            _idle_in_transaction(wire)
            started = time.monotonic()
            # Nothing is sent: the error arrives on its own once the client has idled past the timeout.
            kind, body = wire.read()
            elapsed = time.monotonic() - started
            assert kind == b"E"
            assert (error_fields(body)["V"], error_fields(body)["C"]) == ("FATAL", "58000")
            assert 1.0 <= elapsed <= 6
            assert wire.read() == (b"Z", b"I")


def test_idle_transaction_timeout_rolls_back_and_keeps_the_session(tenants):
    with run_pooler(pools=tenant_pools(tenants), general={"idle_client_in_transaction_timeout": 1500}) as pooler:
        with _wire_session(pooler.port) as wire:
            _idle_in_transaction(wire)
            assert wire.read()[0] == b"E"
            assert wire.read() == (b"Z", b"I")
            assert wait_until(lambda: alpha_rows(tenants) == [
                (tenants.alpha.owner, tenants.alpha.database, "idle")], timeout=8)
            assert tenants.ledger(tenants.alpha) == []
            reply = wire.query("SELECT count(*) FROM ledger")
            assert [kind for kind, _ in reply] == [b"T", b"D", b"C", b"Z"]
            assert reply[1][1] == struct.pack("!hi", 1, 1) + b"0"


def test_transactions_shorter_than_the_idle_timeout_commit(tenants):
    with run_pooler(pools=tenant_pools(tenants), general={"idle_client_in_transaction_timeout": 3000}) as pooler:
        with pooler.alpha() as conn:
            conn.execute("BEGIN")
            conn.execute("INSERT INTO ledger VALUES ('quick', 1)")
            time.sleep(0.5)
            conn.execute("COMMIT")
    assert tenants.ledger(tenants.alpha) == [("quick", 1)]


def _wire_session(port):
    wire = Wire(port, timeout=15)
    wire.startup(user="app", database="alpha")
    _, body = wire.read()
    wire.send(b"p", md5_response("app", "alpha-secret", body[4:8]))
    wire.until_ready()
    return wire


def test_statement_timeout_ends_the_client_session(tenants):
    pools = tenant_pools(tenants)
    pools["alpha"]["users"][0]["statement_timeout"] = 1500
    with run_pooler(pools=pools) as pooler:
        with _wire_session(pooler.port) as wire:
            started = time.monotonic()
            wire.send(b"Q", b"SELECT pg_sleep(8)\0")
            messages = [m for m in wire.until_closed() if isinstance(m, tuple)]
            elapsed = time.monotonic() - started
        errors = [error_fields(body) for kind, body in messages if kind == b"E"]
        assert [(e["V"], e["C"]) for e in errors] == [("FATAL", "58000")]
        assert not any(kind == b"Z" for kind, _ in messages)
        assert 1.0 <= elapsed <= 6


def test_statement_timeout_leaves_quick_queries_and_other_pools_alone(tenants):
    pools = tenant_pools(tenants)
    pools["alpha"]["users"][0]["statement_timeout"] = 1500
    with run_pooler(pools=pools) as pooler:
        with pooler.alpha() as a, pooler.beta() as b:
            assert a.execute("SELECT 'quick' FROM pg_sleep(0.3)").fetchall() == [("quick",)]
            assert b.execute("SELECT 'slow' FROM pg_sleep(2.5)").fetchall() == [("slow",)]


def test_idle_server_connections_close_after_idle_timeout(tenants):
    pools = tenant_pools(tenants, alpha_settings={"idle_timeout": 1000})
    with run_pooler(pools=pools) as pooler:
        with pooler.alpha() as a, pooler.alpha() as b:
            a.execute("BEGIN")
            b.execute("SELECT 1")
            a.execute("COMMIT")
        assert len(alpha_rows(tenants)) == 2
        assert wait_until(lambda: alpha_rows(tenants) == [], timeout=10)
        with pooler.alpha() as conn:
            assert conn.execute("SELECT (SELECT tenant FROM marker)").fetchall() == [("alpha",)]


def test_server_lifetime_replaces_old_connections(tenants):
    with run_pooler(pools=tenant_pools(tenants), general={"server_lifetime": 1000}) as pooler:
        with pooler.alpha() as conn:
            first = conn.execute("SELECT pg_backend_pid()").fetchone()[0]
            assert wait_until(lambda: tenants.engines[0].query(
                f"SELECT count(*) FROM pg_stat_activity WHERE pid = {first}") == [(0,)], timeout=10)
            assert conn.execute("SELECT pg_backend_pid()").fetchone()[0] != first


def test_min_pool_size_opens_server_connections_before_any_client(tenants):
    pools = tenant_pools(tenants, alpha_size=3)
    pools["alpha"]["users"][0]["min_pool_size"] = 3
    with run_pooler(pools=pools):
        assert wait_until(lambda: alpha_rows(tenants) == [
            (tenants.alpha.owner, tenants.alpha.database, "idle")] * 3, timeout=10)
