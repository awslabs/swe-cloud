"""The pooler's own SET/SHOW routing commands: server role, primary reads and shard selection."""

from collections import Counter

import psycopg
import pytest

from conftest import marker, run_pooler, tenant_pools


def single(conn, sql):
    cursor = conn.execute(sql)
    return [column.name for column in cursor.description], cursor.fetchall(), cursor.statusmessage


def test_show_server_role_defaults_to_the_pool_role(tenants, pooler):
    with pooler.alpha() as conn:
        assert single(conn, "SHOW SERVER ROLE") == (["server role"], [("any",)], "SELECT 1")


def test_set_server_role_is_acknowledged_and_reported(tenants, pooler):
    with pooler.alpha() as conn:
        assert conn.execute("SET SERVER ROLE TO 'primary'").statusmessage == "SET SERVER ROLE"
        assert conn.execute("SHOW SERVER ROLE").fetchall() == [("primary",)]
        assert conn.execute("SELECT (SELECT tenant FROM marker)").fetchall() == [("alpha",)]


def test_server_role_default_restores_the_configured_role(tenants):
    with run_pooler(pools=tenant_pools(tenants, alpha_settings={"default_role": "primary"})) as pooler:
        with pooler.alpha() as conn:
            assert conn.execute("SHOW SERVER ROLE").fetchall() == [("primary",)]
            conn.execute("SET SERVER ROLE TO 'any'")
            assert conn.execute("SHOW SERVER ROLE").fetchall() == [("any",)]
            conn.execute("SET SERVER ROLE TO 'default'")
            assert conn.execute("SHOW SERVER ROLE").fetchall() == [("primary",)]


def test_server_role_without_matching_server_fails_checkout(tenants, pooler):
    with pooler.alpha() as conn:
        conn.execute("SET SERVER ROLE TO 'replica'")
        with pytest.raises(psycopg.Error) as failed:
            conn.execute("SELECT 1")
        assert (failed.value.sqlstate, failed.value.diag.severity_nonlocalized) == ("58000", "FATAL")
        conn.execute("SET SERVER ROLE TO 'primary'")
        assert conn.execute("SELECT 'back'").fetchall() == [("back",)]


def test_server_role_setting_belongs_to_one_client(tenants, pooler):
    with pooler.alpha() as conn, pooler.alpha() as other:
        conn.execute("SET SERVER ROLE TO 'replica'")
        assert other.execute("SHOW SERVER ROLE").fetchall() == [("any",)]
        assert other.execute("SELECT 1").fetchall() == [(1,)]


def test_server_role_routes_to_the_chosen_server(tenants, replica_servers):
    with run_pooler(pools=tenant_pools(tenants, alpha_servers=replica_servers)) as pooler:
        with pooler.alpha() as conn:
            conn.execute("SET SERVER ROLE TO 'replica'")
            assert Counter(marker(conn) for _ in range(12)) == Counter({"alpha@2": 12})
            conn.execute("SET SERVER ROLE TO 'primary'")
            assert Counter(marker(conn) for _ in range(12)) == Counter({"alpha": 12})


def test_show_and_set_primary_reads(tenants, pooler):
    with pooler.alpha() as conn:
        assert single(conn, "SHOW PRIMARY READS") == (["primary reads"], [("off",)], "SELECT 1")
        assert conn.execute("SET PRIMARY READS TO 'on'").statusmessage == "SET PRIMARY READS"
        assert conn.execute("SHOW PRIMARY READS").fetchall() == [("on",)]
        conn.execute("SET PRIMARY READS TO default")
        assert conn.execute("SHOW PRIMARY READS").fetchall() == [("off",)]


def test_show_shard_is_unset_until_chosen(tenants, pooler):
    with pooler.alpha() as conn:
        assert single(conn, "SHOW SHARD") == (["shard"], [("unset",)], "SELECT 1")
        assert conn.execute("SET SHARD TO '0'").statusmessage == "SET SHARD"
        assert conn.execute("SHOW SHARD").fetchall() == [("0",)]


def test_unconfigured_shard_is_refused_and_selection_kept(tenants, pooler):
    with pooler.alpha() as conn:
        conn.execute("SET SHARD TO '0'")
        with pytest.raises(psycopg.Error) as failed:
            conn.execute("SET SHARD TO '1'")
        assert (failed.value.sqlstate, failed.value.diag.severity_nonlocalized) == ("58000", "FATAL")
        assert conn.execute("SHOW SHARD").fetchall() == [("0",)]


def test_routing_commands_ignore_case(tenants, pooler):
    with pooler.alpha() as conn:
        assert conn.execute("set server role to 'primary';").statusmessage == "SET SERVER ROLE"
        assert conn.execute("show server role").fetchall() == [("primary",)]


def test_unquoted_server_role_reaches_the_server_as_sql(tenants, pooler):
    with pooler.alpha() as conn:
        with pytest.raises(psycopg.Error) as failed:
            conn.execute("SET SERVER ROLE TO primary")
        assert (failed.value.sqlstate, failed.value.diag.severity_nonlocalized) == ("42601", "ERROR")
        assert conn.execute("SHOW SERVER ROLE").fetchall() == [("any",)]


def test_routing_commands_inside_a_transaction_reach_the_server(tenants, pooler):
    with pooler.alpha() as conn:
        conn.execute("BEGIN")
        conn.execute("SELECT 1")
        with pytest.raises(psycopg.Error) as failed:
            conn.execute("SHOW SERVER ROLE")
        assert failed.value.sqlstate == "42601"
        assert conn.info.transaction_status == psycopg.pq.TransactionStatus.INERROR
        conn.execute("ROLLBACK")
        assert conn.execute("SHOW SERVER ROLE").fetchall() == [("any",)]
