"""One tenant shard with two RDS instances: transactions spread over the eligible servers per default_role."""

from collections import Counter

import psycopg
import pytest

from conftest import run_pooler, tenant_pools


@pytest.fixture
def two_servers(tenants):
    tenants.provision(tenants.engines[1], "@2")
    first, second = tenants.engines
    return [[first.engine_host, first.engine_port, "primary"], [second.engine_host, second.engine_port, "replica"]]


def markers(conn, count):
    return Counter(conn.execute("SELECT tenant FROM marker").fetchone()[0] for _ in range(count))


def balanced_pooler(tenants, servers, role):
    settings = {} if role is None else {"default_role": role}
    return run_pooler(pools=tenant_pools(tenants, alpha_servers=servers, alpha_settings=settings))


@pytest.mark.parametrize("role", ["any", None])
def test_reads_spread_over_primary_and_replica(tenants, two_servers, role):
    with balanced_pooler(tenants, two_servers, role) as pooler:
        with pooler.alpha() as conn:
            seen = markers(conn, 40)
        assert set(seen) == {"alpha", "alpha@2"}
        assert min(seen.values()) >= 4
        per_client = Counter()
        for _ in range(24):
            with pooler.alpha() as conn:
                per_client.update(markers(conn, 1))
        assert set(per_client) == {"alpha", "alpha@2"}
        with pooler.beta() as beta:
            assert markers(beta, 10) == Counter({"beta": 10})


def test_transaction_stays_on_one_server(tenants, two_servers):
    with balanced_pooler(tenants, two_servers, "any") as pooler:
        with pooler.alpha() as conn:
            firsts = Counter()
            for _ in range(24):
                conn.execute("BEGIN")
                first = conn.execute("SELECT tenant FROM marker").fetchone()[0]
                firsts[first] += 1
                assert markers(conn, 4) == Counter({first: 4})
                conn.execute("COMMIT")
            assert set(firsts) == {"alpha", "alpha@2"}


@pytest.mark.parametrize(("role", "only"), [("primary", "alpha"), ("replica", "alpha@2")])
def test_default_role_restricts_eligible_servers(tenants, two_servers, role, only):
    with balanced_pooler(tenants, two_servers, role) as pooler:
        with pooler.alpha() as conn:
            assert markers(conn, 24) == Counter({only: 24})
        for _ in range(6):
            with pooler.alpha() as conn:
                assert markers(conn, 1) == Counter({only: 1})
        with pooler.beta() as beta:
            assert markers(beta, 6) == Counter({"beta": 6})


def test_pool_size_applies_to_each_server(tenants, two_servers):
    with balanced_pooler(tenants, two_servers, "any") as pooler:
        holders = [pooler.alpha() for _ in range(4)]
        extra = pooler.alpha()
        try:
            served = Counter()
            for conn in holders:
                conn.execute("BEGIN")
                served.update(markers(conn, 1))
            assert served == Counter({"alpha": 2, "alpha@2": 2})
            for engine in tenants.engines:
                rows = engine.query(f"SELECT state FROM pg_stat_activity WHERE usename = '{tenants.alpha.owner}' "
                                    "AND state = 'idle in transaction'")
                assert rows == [("idle in transaction",)] * 2
            with pytest.raises(psycopg.Error) as failed:
                extra.execute("SELECT tenant FROM marker")
            assert failed.value.sqlstate == "58000"
        finally:
            for conn in holders + [extra]:
                conn.close()
