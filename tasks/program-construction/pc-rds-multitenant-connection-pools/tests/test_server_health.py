"""Unreachable servers: replicas are banned and skipped, an unreachable primary fails the pool; least-busy balancing."""

import socket
from collections import Counter

import psycopg
import pytest

from conftest import marker, run_pooler, show_rows, tenant_pools
from pgwire import refusal


def touch_until_banned(pooler, conn, limit=60):
    """Run queries until the admin console lists a ban (server choice is random per checkout)."""
    with pooler.admin() as admin:
        for _ in range(limit):
            assert marker(conn) == "alpha"
            if show_rows(admin, "SHOW BANS")[1]:
                return


@pytest.fixture
def dead_port():
    """A local port that refuses connections: bound but not listening, so nothing else can take it."""
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        yield held.getsockname()[1]


@pytest.fixture
def dead_replica(tenants, dead_port):
    engine = tenants.engines[0]
    return [[engine.engine_host, engine.engine_port, "primary"], ["127.0.0.1", dead_port, "replica"]]


def test_unreachable_replica_is_skipped(tenants, dead_replica):
    with run_pooler(pools=tenant_pools(tenants, alpha_servers=dead_replica)) as pooler:
        with pooler.alpha() as conn:
            assert Counter(marker(conn) for _ in range(16)) == Counter({"alpha": 16})


def test_unreachable_replica_is_listed_in_show_bans(tenants, dead_replica):
    with run_pooler(pools=tenant_pools(tenants, alpha_servers=dead_replica)) as pooler:
        with pooler.alpha() as conn:
            touch_until_banned(pooler, conn)
        with pooler.admin() as admin:
            names, rows = show_rows(admin, "SHOW BANS")
    assert names == ["db", "user", "role", "host", "reason", "ban_time", "ban_duration_seconds", "ban_remaining_seconds"]
    assert [row[:4] + (row[6],) for row in rows] == [("alpha", "app", "replica", "127.0.0.1", "60")]


def test_ban_time_sets_the_ban_duration(tenants, dead_replica):
    with run_pooler(pools=tenant_pools(tenants, alpha_servers=dead_replica), general={"ban_time": 300}) as pooler:
        with pooler.alpha() as conn:
            touch_until_banned(pooler, conn)
        with pooler.admin() as admin:
            rows = show_rows(admin, "SHOW BANS")[1]
    assert [(row[3], row[6]) for row in rows] == [("127.0.0.1", "300")]
    assert 290 <= int(rows[0][7]) <= 300


def test_banned_replica_is_disabled_in_show_databases(tenants, dead_replica):
    with run_pooler(pools=tenant_pools(tenants, alpha_servers=dead_replica)) as pooler:
        with pooler.alpha() as conn:
            touch_until_banned(pooler, conn)
        with pooler.admin() as admin:
            rows = show_rows(admin, "SHOW DATABASES")[1]
    disabled = {row[0]: row[12] for row in rows}
    assert disabled == {"alpha_shard_0_primary": 0, "alpha_shard_0_replica_0": 1, "beta_shard_0_primary": 0}


def test_unban_lifts_the_ban(tenants, dead_replica):
    with run_pooler(pools=tenant_pools(tenants, alpha_servers=dead_replica)) as pooler:
        with pooler.alpha() as conn:
            touch_until_banned(pooler, conn)
        with pooler.admin() as admin:
            cursor = admin.execute("UNBAN 127.0.0.1")
            assert cursor.statusmessage == "UNBAN"
            assert cursor.fetchall() == [("alpha", "app", "replica", "127.0.0.1")]
            assert show_rows(admin, "SHOW BANS")[1] == []


def test_unreachable_primary_refuses_logins_after_the_password(tenants, dead_port):
    servers = [["127.0.0.1", dead_port, "primary"]]
    with run_pooler(pools=tenant_pools(tenants, alpha_servers=servers)) as pooler:
        # The password exchange completes, then the login fails without AuthenticationOk.
        assert refusal(pooler.port, "alpha", "app", "alpha-secret")[:2] == ([5], [("FATAL", "58000")])
        with pytest.raises(psycopg.OperationalError):
            pooler.alpha()
        with pooler.beta() as conn:
            assert marker(conn) == "beta"


def test_least_outstanding_connections_avoids_a_busy_server(tenants, replica_servers):
    pools = tenant_pools(tenants, alpha_servers=replica_servers, alpha_settings={"load_balancing_mode": "loc"})
    with run_pooler(pools=pools) as pooler:
        with pooler.alpha() as holder, pooler.alpha() as conn:
            holder.execute("BEGIN")
            busy = marker(holder)
            idle = {"alpha": "alpha@2", "alpha@2": "alpha"}[busy]
            assert Counter(marker(conn) for _ in range(12)) == Counter({idle: 12})
            holder.execute("COMMIT")
