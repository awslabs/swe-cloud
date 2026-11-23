"""Invocation, queue-table setup, startup failures and shutdown."""

import signal
import subprocess

import pytest

import broker as support
from broker import row
from harness import run_program, workspace


def _columns(mysql, table):
    return {
        name: kind
        for name, kind in support.query(
            mysql, "SELECT COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
            mysql["db"], table,
        )
    }


def _unique_indexes(mysql, table):
    found = {}
    for index, column, position in support.query(
        mysql,
        "SELECT INDEX_NAME, COLUMN_NAME, SEQ_IN_INDEX FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND NON_UNIQUE=0",
        mysql["db"], table,
    ):
        found.setdefault(index, []).append((position, column))
    return [frozenset(column for _, column in columns) for columns in found.values()]


def test_creates_queue_table_at_startup(mysql, make_broker, table):
    assert not support.table_exists(mysql, table)
    server = make_broker(table)
    columns = _columns(mysql, table)
    assert set(columns) == set(support.QUEUE_COLUMNS), columns
    integers = {"tinyint", "smallint", "mediumint", "int", "bigint"}
    assert columns["priority"] in integers and columns["when_to_run"] in integers, columns
    assert columns["data"] in {"blob", "mediumblob", "longblob"}, columns
    assert frozenset({"unique_key", "function_name"}) in _unique_indexes(mysql, table)
    assert support.rows(mysql, table) == []
    # The server answers on its port and the new table receives background jobs.
    conn = server.conn()
    conn.send("ECHO_REQ", b"\0ping\xff")
    assert conn.recv() == ("ECHO_RES", b"\0ping\xff")
    conn.submit_ok("SUBMIT_JOB_BG", "created", "c-1", b"payload")
    server.expect_rows(row("c-1", "created", 1, b"payload"))


def test_existing_table_is_used_as_is(mysql, make_broker, table):
    support.query(
        mysql,
        f"CREATE TABLE `{table}` (id INT AUTO_INCREMENT PRIMARY KEY, note VARCHAR(16) DEFAULT 'sentinel', "
        "when_to_run BIGINT, data LONGBLOB, priority INT, function_name VARCHAR(255), unique_key VARCHAR(64), "
        "UNIQUE KEY (unique_key, function_name))",
    )
    support.query(mysql, f"INSERT INTO `{table}` (note, unique_key, function_name, priority, data, when_to_run) "
                         "VALUES ('kept', 'old', 'untouched', 2, 'old-data', 0)")
    before = _columns(mysql, table)
    server = make_broker(table)
    assert _columns(mysql, table) == before
    conn = server.conn()
    conn.submit_ok("SUBMIT_JOB_HIGH_BG", "fresh", "new-1", b"new-data")
    got = support.query(mysql, f"SELECT id, note, unique_key, function_name, priority, data, when_to_run FROM `{table}` ORDER BY id")
    assert got == [(1, "kept", "old", "untouched", 2, b"old-data", 0), (2, "sentinel", "new-1", "fresh", 0, b"new-data", 0)]


def _failed_start(mysql, table, **overrides):
    settings = {**mysql, **overrides}
    with workspace() as directory:
        args = support.program_args(settings, table, support.free_port(), directory)
        try:
            result = run_program(args, cwd=directory, timeout=30)
        except subprocess.TimeoutExpired:
            pytest.fail("server kept running although its queue could not be initialised")
    assert result.returncode != 0
    assert result.stderr.strip(), "no error reported on standard error"


def test_startup_fails_with_bad_credentials(mysql, table):
    _failed_start(mysql, table, password="not-the-password")
    assert not support.table_exists(mysql, table)


def test_startup_fails_without_database(mysql, table):
    _failed_start(mysql, table, db="missing_db_" + table)
    assert not support.table_exists(mysql, table)


def test_startup_fails_when_table_lacks_a_column(mysql, table):
    support.query(mysql, f"CREATE TABLE `{table}` (unique_key VARCHAR(64), function_name VARCHAR(255), priority INT, "
                         "data LONGBLOB, UNIQUE KEY (unique_key, function_name))")
    support.query(mysql, f"INSERT INTO `{table}` VALUES ('u', 'f', 1, 'd')")
    _failed_start(mysql, table)
    assert set(_columns(mysql, table)) == {"unique_key", "function_name", "priority", "data"}
    assert support.query(mysql, f"SELECT * FROM `{table}`") == [("u", "f", 1, b"d")]


def test_equals_spelling_and_default_table_name(mysql, make_broker):
    support.drop_table(mysql, "gearman_queue")
    try:
        server = make_broker(None, equals=True, extra=[("--verbose", "INFO"), ("--job-handle-prefix", "Q:graded")])
        assert support.table_exists(mysql, "gearman_queue")
        conn = server.conn()
        handles = {conn.submit_ok("SUBMIT_JOB_LOW_BG", "defaulted", "d-1", b"in default table"),
                   conn.submit_ok("SUBMIT_JOB", "defaulted", "d-2", b"foreground")}
        assert len(handles) == 2 and all(h.startswith(b"Q:graded:") and len(h) > 9 for h in handles), handles
        assert support.rows(mysql, "gearman_queue") == [row("d-1", "defaulted", 2, b"in default table")]
    finally:
        support.drop_table(mysql, "gearman_queue")


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_signal_shuts_down_cleanly_and_keeps_rows(server, sig):
    conn = server.conn()
    conn.submit_ok("SUBMIT_JOB_BG", "durable", "s-1", b"kept")
    assert server.stop(sig) == 0
    server.expect_rows(row("s-1", "durable", 1, b"kept"))
