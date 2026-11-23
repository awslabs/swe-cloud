"""Query-parser read/write splitting over a primary and a replica RDS instance."""

from collections import Counter

import pytest

from conftest import marker, run_pooler, tenant_pools

SPLIT = {"query_parser_enabled": True, "query_parser_read_write_splitting": True}


@pytest.fixture
def split_pooler(tenants, replica_servers):
    with run_pooler(pools=tenant_pools(tenants, alpha_servers=replica_servers, alpha_settings=SPLIT)) as pooler:
        yield pooler


def reads(conn, sql="SELECT tenant FROM marker", count=16):
    return Counter(conn.execute(sql).fetchone()[0] for _ in range(count))


def test_reads_go_to_the_replica(tenants, split_pooler):
    with split_pooler.alpha() as conn:
        assert reads(conn) == Counter({"alpha@2": 16})


def test_writes_go_to_the_primary(tenants, split_pooler):
    with split_pooler.alpha() as conn:
        for index in range(6):
            assert conn.execute(f"INSERT INTO ledger VALUES ('w{index}', {index})").statusmessage == "INSERT 0 1"
    assert tenants.ledger(tenants.alpha) == [(f"w{index}", index) for index in range(6)]
    assert tenants.ledger(tenants.alpha, tenants.engines[1]) == []


def test_locking_reads_go_to_the_primary(tenants, split_pooler):
    with split_pooler.alpha() as conn:
        assert reads(conn, "SELECT tenant FROM marker FOR UPDATE", 10) == Counter({"alpha": 10})


def test_transactions_go_to_the_primary(tenants, split_pooler):
    with split_pooler.alpha() as conn:
        served = Counter()
        for _ in range(8):
            conn.execute("BEGIN")
            served[marker(conn)] += 1
            conn.execute("COMMIT")
        assert served == Counter({"alpha": 8})


def test_query_string_with_a_write_goes_to_the_primary(tenants, split_pooler):
    with split_pooler.alpha() as conn:
        cursor = conn.cursor()
        served = Counter()
        for _ in range(8):
            cursor.execute("INSERT INTO ledger SELECT 'never', 1 WHERE false; SELECT tenant FROM marker")
            assert cursor.nextset()
            served[cursor.fetchone()[0]] += 1
        assert served == Counter({"alpha": 8})


def test_show_server_role_reports_auto_with_the_parser(tenants, split_pooler):
    with split_pooler.alpha() as conn:
        assert conn.execute("SHOW SERVER ROLE").fetchall() == [("auto",)]


def test_primary_reads_setting_spreads_reads(tenants, replica_servers):
    settings = {**SPLIT, "primary_reads_enabled": True}
    with run_pooler(pools=tenant_pools(tenants, alpha_servers=replica_servers, alpha_settings=settings)) as pooler:
        with pooler.alpha() as conn:
            assert conn.execute("SHOW PRIMARY READS").fetchall() == [("on",)]
            assert set(reads(conn, count=40)) == {"alpha", "alpha@2"}
            assert reads(conn, "SELECT tenant FROM marker FOR UPDATE", 8) == Counter({"alpha": 8})


def test_set_primary_reads_on_spreads_reads_for_that_client(tenants, split_pooler):
    with split_pooler.alpha() as conn, split_pooler.alpha() as other:
        conn.execute("SET PRIMARY READS TO on")
        assert set(reads(conn, count=40)) == {"alpha", "alpha@2"}
        assert reads(other) == Counter({"alpha@2": 16})


def test_server_role_any_turns_off_splitting_for_that_client(tenants, split_pooler):
    with split_pooler.alpha() as conn:
        conn.execute("SET SERVER ROLE TO 'any'")
        assert set(reads(conn, "SELECT tenant FROM marker FOR UPDATE", 40)) == {"alpha", "alpha@2"}
        conn.execute("SET SERVER ROLE TO 'auto'")
        assert reads(conn) == Counter({"alpha@2": 16})
