"""Extended query protocol through the pooler: parameterised queries, binary results and prepared statements."""

import psycopg
import pytest
from psycopg.pq import TransactionStatus

from conftest import run_pooler, tenant_pools

pytestmark = pytest.mark.usefixtures("tenants")


def extended(pooler, dbname="alpha", password="alpha-secret", **kwargs):
    """A psycopg session with its default (extended protocol) cursors."""
    return psycopg.connect(host="127.0.0.1", port=pooler.port, dbname=dbname, user="app", password=password,
                           autocommit=True, sslmode="disable", connect_timeout=15, **kwargs)


def test_parameterised_query_returns_rows(tenants, pooler):
    with extended(pooler) as conn:
        cursor = conn.execute("SELECT %s::int + 1 AS n, %s::text AS t, (SELECT tenant FROM marker) AS m", (41, "zß€"))
        assert [column.name for column in cursor.description] == ["n", "t", "m"]
        assert cursor.fetchall() == [(42, "zß€", "alpha")]


def test_parameterised_writes_reach_the_tenant_database(tenants, pooler):
    with extended(pooler, "beta", "beta-secret") as conn:
        for index in range(3):
            assert conn.execute("INSERT INTO ledger VALUES (%s, %s)", (f"p{index}", index)).rowcount == 1
    assert tenants.ledger(tenants.beta) == [("p0", 0), ("p1", 1), ("p2", 2)]
    assert tenants.ledger(tenants.alpha) == []


def test_extended_protocol_error_keeps_the_session(tenants, pooler):
    with extended(pooler) as conn:
        with pytest.raises(psycopg.Error) as failed:
            conn.execute("SELECT %s::int / 0", (1,))
        assert (failed.value.sqlstate, failed.value.diag.severity_nonlocalized) == ("22012", "ERROR")
        assert conn.info.transaction_status == TransactionStatus.IDLE
        assert conn.execute("SELECT %s::text || 'x'", ("ok",)).fetchall() == [("okx",)]


def test_binary_parameters_and_results(tenants, pooler):
    with extended(pooler) as conn:
        rows = conn.execute("SELECT %s::int8 * 2, %s::bytea, %s::float8", (2**40, b"\x00\xff\x01", 0.5),
                            binary=True).fetchall()
        assert rows == [(2**41, b"\x00\xff\x01", 0.5)]


def test_extended_transaction_stays_on_one_server(tenants, pooler):
    with extended(pooler) as conn, pooler.alpha() as other:
        conn.execute("BEGIN")
        pid = conn.execute("SELECT pg_backend_pid()").fetchone()[0]
        conn.execute("INSERT INTO ledger VALUES (%s, %s)", ("x", 1))
        assert conn.info.transaction_status == TransactionStatus.INTRANS
        assert other.execute("SELECT count(*) FROM ledger").fetchall() == [(0,)]
        assert conn.execute("SELECT pg_backend_pid(), count(*) FROM ledger").fetchall() == [(pid, 1)]
        conn.execute("ROLLBACK")
    assert tenants.ledger(tenants.alpha) == []


def test_prepared_statements_work_across_server_connections_with_a_cache(tenants):
    pools = tenant_pools(tenants, alpha_settings={"prepared_statements_cache_size": 64})
    with run_pooler(pools=pools) as pooler:
        with extended(pooler, prepare_threshold=0) as a, extended(pooler, prepare_threshold=0) as b:
            for index in range(8):
                assert a.execute("SELECT %s::int * 2", (index,)).fetchall() == [(index * 2,)]
                assert b.execute("SELECT %s::int + 100", (index,)).fetchall() == [(index + 100,)]
                assert b.execute("SELECT %s::int * 2", (index + 1,)).fetchall() == [((index + 1) * 2,)]


def test_prepared_statement_inside_a_transaction_with_a_cache(tenants):
    pools = tenant_pools(tenants, alpha_settings={"prepared_statements_cache_size": 64})
    with run_pooler(pools=pools) as pooler:
        with extended(pooler, prepare_threshold=0) as conn:
            conn.execute("BEGIN")
            for index in range(3):
                conn.execute("INSERT INTO ledger VALUES (%s, %s)", (f"tx{index}", index))
            conn.execute("COMMIT")
            assert conn.execute("SELECT count(*) FROM ledger WHERE v >= %s", (0,)).fetchall() == [(3,)]
    assert tenants.ledger(tenants.alpha) == [("tx0", 0), ("tx1", 1), ("tx2", 2)]
