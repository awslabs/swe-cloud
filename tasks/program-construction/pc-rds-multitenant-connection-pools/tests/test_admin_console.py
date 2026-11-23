"""The admin console database: login, SHOW listings and errors for unsupported commands."""

import psycopg
import pytest

from conftest import show_rows
from pgwire import refusal

pytestmark = pytest.mark.usefixtures("tenants")


def test_admin_console_accepts_admin_credentials(tenants, pooler):
    with pooler.admin() as admin:
        assert admin.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
        assert admin.info.parameter_status("client_encoding") == "UTF8"


def test_admin_console_refuses_wrong_password(tenants, pooler):
    codes, errors, kinds = refusal(pooler.port, "pgcat", pooler.general["admin_username"], "not-the-admin-password")
    assert (codes, errors) == ([5], [("FATAL", "28P01")])
    assert b"Z" not in kinds


def test_admin_console_refuses_other_users(tenants, pooler):
    # A tenant user with its own password is not the admin user.
    codes, errors, _ = refusal(pooler.port, "pgcat", "app", "alpha-secret")
    assert (codes, errors) == ([5], [("FATAL", "28P01")])


def test_show_version_returns_one_text_row(tenants, pooler):
    with pooler.admin() as admin:
        cursor = admin.execute("SHOW VERSION")
        assert [column.name for column in cursor.description] == ["version"]
        rows = cursor.fetchall()
        assert len(rows) == 1 and rows[0][0]
        assert cursor.statusmessage == "SHOW"


def test_show_help_sends_a_notice_and_no_rows(tenants, pooler):
    with pooler.admin() as admin:
        notices = []
        admin.add_notice_handler(lambda diag: notices.append((diag.severity, diag.sqlstate)))
        cursor = admin.execute("SHOW HELP")
        assert cursor.description is None
        assert cursor.statusmessage == "SHOW"
        assert notices == [("NOTICE", "00000")]


def test_show_databases_lists_each_configured_server(tenants, pooler):
    with pooler.admin() as admin:
        names, rows = show_rows(admin, "SHOW DATABASES")
    assert names == ["name", "host", "port", "database", "force_user", "pool_size", "min_pool_size", "reserve_pool",
                     "pool_mode", "max_connections", "current_connections", "paused", "disabled"]
    engine = tenants.engines[0]
    picked = sorted((row[0], row[1], row[2], row[3], row[4], row[5], row[8], row[9], row[11], row[12]) for row in rows)
    assert picked == [
        ("alpha_shard_0_primary", engine.engine_host, str(engine.engine_port), tenants.alpha.database, "app", 2,
         "transaction", 2, 0, 0),
        ("beta_shard_0_primary", engine.engine_host, str(engine.engine_port), tenants.beta.database, "app", 3,
         "transaction", 3, 0, 0),
    ]


def test_show_pools_lists_each_pool_and_user(tenants, pooler):
    with pooler.alpha() as conn:
        conn.execute("SELECT 1")
        with pooler.admin() as admin:
            names, rows = show_rows(admin, "SHOW POOLS")
    assert names == ["database", "user", "pool_mode", "cl_idle", "cl_active", "cl_waiting", "cl_cancel_req",
                     "sv_active", "sv_idle", "sv_used", "sv_tested", "sv_login", "maxwait", "maxwait_us"]
    assert sorted(row[:3] for row in rows) == [("alpha", "app", "transaction"), ("beta", "app", "transaction")]


def test_show_pools_counts_idle_clients_and_active_servers(tenants, pooler):
    idle = [pooler.beta() for _ in range(2)]
    holder = pooler.alpha()
    try:
        holder.execute("BEGIN")
        holder.execute("SELECT 1")
        with pooler.admin() as admin:
            _, rows = show_rows(admin, "SHOW POOLS")
        by_pool = {row[0]: row for row in rows}
        assert int(by_pool["beta"][3]) == 2       # cl_idle
        assert int(by_pool["alpha"][4]) == 1      # cl_active
        assert int(by_pool["alpha"][7]) == 1      # sv_active
    finally:
        for conn in idle + [holder]:
            conn.close()


def test_show_users_lists_one_row_per_pool_user(tenants, pooler):
    with pooler.admin() as admin:
        names, rows = show_rows(admin, "SHOW USERS")
    assert names == ["name", "pool_mode"]
    assert rows == [("app", "transaction"), ("app", "transaction")]


def test_show_config_reports_effective_settings(tenants, pooler):
    with pooler.admin() as admin:
        names, rows = show_rows(admin, "SHOW CONFIG")
    assert names == ["key", "value", "default", "changeable"]
    config = {row[0]: (row[1], row[3]) for row in rows}
    assert config["port"] == (str(pooler.port), "no")
    assert config["host"] == ("127.0.0.1", "no")
    assert config["connect_timeout"] == ("1000", "no")
    assert config["pools.alpha.pool_mode"] == ("transaction", "yes")
    assert config["pools.beta.default_role"] == ("any", "yes")


def test_show_clients_lists_connected_clients(tenants, pooler):
    with pooler.alpha(application_name="ledger-ui") as a, pooler.beta(application_name="report-job") as b:
        a.execute("SELECT 1")
        b.execute("SELECT 1")
        with pooler.admin(application_name="console") as admin:
            names, rows = show_rows(admin, "SHOW CLIENTS")
    assert names == ["client_id", "database", "user", "application_name", "state", "transaction_count", "query_count",
                     "error_count", "age_seconds", "maxwait", "maxwait_us"]
    assert sorted(row[1:5] for row in rows) == [
        ("alpha", "app", "ledger-ui", "idle"),
        ("beta", "app", "report-job", "idle"),
        ("pgcat", pooler.general["admin_username"], "console", "idle"),
    ]
    assert len({row[0] for row in rows}) == 3


def test_show_clients_counts_autocommit_queries(tenants, pooler):
    with pooler.alpha(application_name="counted") as conn:
        for _ in range(5):
            conn.execute("SELECT 1")
        with pooler.admin() as admin:
            _, rows = show_rows(admin, "SHOW CLIENTS")
    row = next(row for row in rows if row[3] == "counted")
    assert (int(row[5]), int(row[6])) == (5, 5)


def test_show_servers_lists_server_connections(tenants, pooler):
    holders = [pooler.alpha(application_name="holder") for _ in range(2)]
    try:
        for conn in holders:
            conn.execute("BEGIN")
            conn.execute("SELECT 1")
        with pooler.admin() as admin:
            names, rows = show_rows(admin, "SHOW SERVERS")
        assert names[:6] == ["server_id", "database_name", "user", "address_id", "application_name", "state"]
        alpha = [row for row in rows if row[1] == "alpha"]
        assert sorted(row[2:6] for row in alpha) == [("app", "alpha_shard_0_primary", "holder", "active")] * 2
    finally:
        for conn in holders:
            conn.close()


def test_show_stats_counts_transactions_per_server(tenants, pooler):
    with pooler.alpha() as conn:
        for _ in range(4):
            conn.execute("SELECT 1")
        with pooler.admin() as admin:
            names, rows = show_rows(admin, "SHOW STATS")
    assert names[:5] == ["instance", "database", "user", "total_xact_count", "total_query_count"]
    stats = {row[0]: row for row in rows}
    assert set(stats) == {"alpha_shard_0_primary", "beta_shard_0_primary"}
    assert (int(stats["alpha_shard_0_primary"][3]), int(stats["alpha_shard_0_primary"][4])) == (4, 4)
    assert (int(stats["beta_shard_0_primary"][3]), int(stats["beta_shard_0_primary"][4])) == (0, 0)


def test_show_lists_names_its_counters(tenants, pooler):
    with pooler.admin() as admin:
        names, rows = show_rows(admin, "SHOW LISTS")
    assert names == ["list", "items"]
    assert {row[0] for row in rows} >= {"databases", "users", "pools", "free_clients", "used_clients",
                                         "free_servers", "used_servers"}


def test_unsupported_admin_query_errors_and_session_continues(tenants, pooler):
    with pooler.admin() as admin:
        for command in ("SELECT 1", "SHOW NOTHING_LIKE_THIS"):
            with pytest.raises(psycopg.Error) as failed:
                admin.execute(command)
            assert (failed.value.sqlstate, failed.value.diag.severity_nonlocalized) == ("58000", "FATAL")
            assert not admin.closed
        assert show_rows(admin, "SHOW USERS")[0] == ["name", "pool_mode"]


def test_admin_set_is_accepted_without_effect(tenants, pooler):
    with pooler.admin() as admin:
        assert admin.execute("SET client_min_messages = debug").statusmessage == "SET"
        assert show_rows(admin, "SHOW USERS")[0] == ["name", "pool_mode"]


def test_admin_commands_ignore_case_and_trailing_semicolon(tenants, pooler):
    with pooler.admin() as admin:
        assert show_rows(admin, "show users;") == show_rows(admin, "SHOW USERS")
