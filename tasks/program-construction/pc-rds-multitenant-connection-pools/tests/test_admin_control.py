"""Admin console control commands: PAUSE/RESUME, BAN/UNBAN argument checks and SHUTDOWN."""

import threading
import time

import psycopg
import pytest

from conftest import show_rows
from pgwire import login, refusal

pytestmark = pytest.mark.usefixtures("tenants")


def _query_in_thread(conn, sql):
    result = {}

    def run():
        try:
            result["rows"] = conn.execute(sql).fetchall()
        except psycopg.Error as exc:
            result["error"] = exc.sqlstate

    thread = threading.Thread(target=run)
    thread.start()
    return thread, result


def test_paused_pool_holds_queries_until_resume(tenants, pooler):
    with pooler.admin() as admin, pooler.alpha() as conn:
        conn.execute("SELECT 1")
        assert admin.execute("PAUSE alpha,app").statusmessage == "PAUSE alpha,app"
        thread, result = _query_in_thread(conn, "SELECT 'after resume'")
        time.sleep(2.5)  # longer than the checkout timeout: a paused pool does not time out the client
        assert result == {}
        assert admin.execute("RESUME alpha,app").statusmessage == "RESUME alpha,app"
        thread.join(10)
        assert result == {"rows": [("after resume",)]}


def test_pausing_one_pool_leaves_others_serving(tenants, pooler):
    with pooler.admin() as admin, pooler.alpha() as a, pooler.beta() as b:
        admin.execute("PAUSE alpha,app")
        thread, result = _query_in_thread(a, "SELECT 1")
        try:
            started = time.monotonic()
            assert b.execute("SELECT (SELECT tenant FROM marker)").fetchall() == [("beta",)]
            assert time.monotonic() - started < 2
            assert result == {}
        finally:
            admin.execute("RESUME alpha,app")
            thread.join(10)
        assert result == {"rows": [(1,)]}


def test_pause_without_arguments_pauses_every_pool(tenants, pooler):
    with pooler.admin() as admin, pooler.beta() as b:
        assert admin.execute("PAUSE").statusmessage == "PAUSE"
        thread, result = _query_in_thread(b, "SELECT 'beta resumed'")
        time.sleep(2)
        assert result == {}
        assert admin.execute("RESUME").statusmessage == "RESUME"
        thread.join(10)
        assert result == {"rows": [("beta resumed",)]}


def test_paused_pool_is_flagged_in_show_databases(tenants, pooler):
    with pooler.admin() as admin:
        admin.execute("PAUSE beta,app")
        paused = {row[0]: row[11] for row in show_rows(admin, "SHOW DATABASES")[1]}
        admin.execute("RESUME beta,app")
        resumed = {row[0]: row[11] for row in show_rows(admin, "SHOW DATABASES")[1]}
    assert paused == {"alpha_shard_0_primary": 0, "beta_shard_0_primary": 1}
    assert resumed == {"alpha_shard_0_primary": 0, "beta_shard_0_primary": 0}


def test_pause_of_unknown_pool_is_an_error(tenants, pooler):
    with pooler.admin() as admin:
        with pytest.raises(psycopg.Error) as failed:
            admin.execute("PAUSE nosuchpool,app")
        assert (failed.value.sqlstate, failed.value.diag.severity_nonlocalized) == ("58000", "FATAL")
        assert not admin.closed


def test_open_transaction_continues_while_its_pool_is_paused(tenants, pooler):
    with pooler.admin() as admin, pooler.alpha() as conn:
        conn.execute("BEGIN")
        conn.execute("INSERT INTO ledger VALUES ('during-pause', 1)")
        admin.execute("PAUSE alpha,app")
        try:
            assert conn.execute("SELECT count(*) FROM ledger").fetchall() == [(1,)]
            conn.execute("COMMIT")
        finally:
            admin.execute("RESUME alpha,app")
    assert tenants.ledger(tenants.alpha) == [("during-pause", 1)]


def test_clients_can_log_in_to_a_paused_pool(tenants, pooler):
    with pooler.admin() as admin:
        admin.execute("PAUSE alpha,app")
        try:
            codes, messages = login(pooler.port, "alpha", "app", "alpha-secret")
        finally:
            admin.execute("RESUME alpha,app")
    assert codes == [5, 0]
    assert messages[-1] == (b"Z", b"I")


@pytest.mark.parametrize("command", ["BAN", "BAN somehost", "BAN somehost soon", "BAN somehost 0", "UNBAN"])
def test_ban_commands_check_their_arguments(tenants, pooler, command):
    with pooler.admin() as admin:
        with pytest.raises(psycopg.Error) as failed:
            admin.execute(command)
        assert (failed.value.sqlstate, failed.value.diag.severity_nonlocalized) == ("58000", "FATAL")
        assert not admin.closed


def test_unban_of_unknown_host_returns_no_rows(tenants, pooler):
    with pooler.admin() as admin:
        cursor = admin.execute("UNBAN 192.0.2.77")
        assert cursor.statusmessage == "UNBAN"
        assert [column.name for column in cursor.description] == ["db", "user", "role", "host"]
        assert cursor.fetchall() == []


def test_shutdown_reports_success_and_closes_idle_clients(tenants, pooler):
    idle = pooler.alpha()
    idle.execute("SELECT 1")
    with pooler.admin() as admin:
        cursor = admin.execute("SHUTDOWN")
        assert [column.name for column in cursor.description] == ["success"]
        assert cursor.fetchall() == [("t",)]
        time.sleep(1)
        with pytest.raises(psycopg.OperationalError):
            idle.execute("SELECT 1")
    idle.close()


def test_shutdown_refuses_new_clients_and_lets_transactions_finish(tenants, pooler):
    busy = pooler.beta()
    busy.execute("BEGIN")
    busy.execute("INSERT INTO ledger VALUES ('late', 1)")
    with pooler.admin() as admin:
        admin.execute("SHUTDOWN")
        time.sleep(1)
        assert refusal(pooler.port, "alpha", "app", "alpha-secret")[:2] == ([], [("FATAL", "58000")])
        # The admin console stays usable while clients drain.
        assert show_rows(admin, "SHOW USERS")[0] == ["name", "pool_mode"]
        busy.execute("COMMIT")
        busy.close()
    assert tenants.ledger(tenants.beta) == [("late", 1)]
    assert pooler.process.wait(15) == 0
