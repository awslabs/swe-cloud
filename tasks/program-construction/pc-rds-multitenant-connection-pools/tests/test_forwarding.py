"""Single sessions: simple-query forwarding, results, server errors, notices and transaction status."""

import psycopg
import pytest
from psycopg.pq import TransactionStatus

pytestmark = pytest.mark.usefixtures("tenants")


def results(cursor):
    """Every result set of a multi-statement simple query: (command tag, column names, rows)."""
    collected = []
    while True:
        names = [column.name for column in cursor.description] if cursor.description else None
        collected.append((cursor.statusmessage, names, cursor.fetchall() if cursor.description else None))
        if not cursor.nextset():
            return collected


def server_error(exc):
    diag = exc.diag
    return exc.sqlstate, diag.severity_nonlocalized, diag.message_primary


def test_multi_statement_query_returns_every_result(tenants, pooler):
    with pooler.alpha() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 AS a, 'x' AS b; SELECT NULL::text AS n, ''::text AS e, 'zß€😀' AS u; "
                       "INSERT INTO ledger VALUES ('m1', 1), ('m2', 2); UPDATE ledger SET v = v + 10 WHERE k = 'm2'")
        assert results(cursor) == [
            ("SELECT 1", ["a", "b"], [(1, "x")]),
            ("SELECT 1", ["n", "e", "u"], [(None, "", "zß€😀")]),
            ("INSERT 0 2", None, None),
            ("UPDATE 1", None, None),
        ]
        assert conn.info.transaction_status == TransactionStatus.IDLE
    assert tenants.ledger(tenants.alpha) == [("m1", 1), ("m2", 12)]
    assert tenants.ledger(tenants.beta) == []


def test_large_results_are_forwarded_completely(tenants, pooler):
    with pooler.beta() as conn:
        rows = conn.execute("SELECT g, repeat(md5(g::text), 16) FROM generate_series(1, 6000) g").fetchall()
        assert len(rows) == 6000
        assert [row[0] for row in rows] == list(range(1, 6001))
        expected_tail = conn.execute("SELECT repeat(md5('6000'), 16)").fetchone()[0]
        assert rows[-1][1] == expected_tail and len(expected_tail) == 512
        big = conn.execute("SELECT length(repeat('y', 3000000)), repeat('z', 3000000)").fetchone()
        assert big[0] == 3000000 and big[1] == "z" * 3000000
        assert conn.execute("SELECT (SELECT tenant FROM marker)").fetchall() == [("beta",)]


def test_server_errors_are_forwarded_and_session_continues(tenants, pooler):
    with pooler.alpha() as conn:
        conn.execute("INSERT INTO ledger VALUES ('dup', 1)")
        cases = [
            ("SELECT * FROM no_such_table", ("42P01", "ERROR", 'relation "no_such_table" does not exist')),
            ("INSERT INTO ledger VALUES ('dup', 2)",
             ("23505", "ERROR", 'duplicate key value violates unique constraint "ledger_pkey"')),
            ("SELECT 1/0", ("22012", "ERROR", "division by zero")),
            ("SELEC 1", ("42601", "ERROR", 'syntax error at or near "SELEC"')),
        ]
        for sql, expected in cases:
            with pytest.raises(psycopg.Error) as failed:
                conn.execute(sql)
            assert server_error(failed.value) == expected
            assert conn.info.transaction_status == TransactionStatus.IDLE
            assert conn.execute("SELECT 'still here'").fetchall() == [("still here",)]
    assert tenants.ledger(tenants.alpha) == [("dup", 1)]


def test_transaction_status_follows_the_server(tenants, pooler):
    with pooler.alpha() as conn:
        conn.execute("BEGIN")
        assert conn.info.transaction_status == TransactionStatus.INTRANS
        conn.execute("INSERT INTO ledger VALUES ('t1', 1)")
        with pytest.raises(psycopg.Error) as failed:
            conn.execute("SELECT 1/0")
        assert failed.value.sqlstate == "22012"
        assert conn.info.transaction_status == TransactionStatus.INERROR
        with pytest.raises(psycopg.Error) as aborted:
            conn.execute("SELECT 1")
        assert aborted.value.sqlstate == "25P02"
        assert conn.info.transaction_status == TransactionStatus.INERROR
        conn.execute("ROLLBACK")
        assert conn.info.transaction_status == TransactionStatus.IDLE
        assert tenants.ledger(tenants.alpha) == []
        # A failed COMMIT ends the transaction as a rollback.
        conn.execute("BEGIN")
        conn.execute("INSERT INTO ledger VALUES ('t2', 2)")
        with pytest.raises(psycopg.Error):
            conn.execute("SELECT * FROM missing_table")
        commit = conn.execute("COMMIT")
        assert commit.statusmessage == "ROLLBACK"
        assert conn.info.transaction_status == TransactionStatus.IDLE
        conn.execute("BEGIN; INSERT INTO ledger VALUES ('t3', 3)")
        assert conn.info.transaction_status == TransactionStatus.INTRANS
        conn.execute("COMMIT")
    assert tenants.ledger(tenants.alpha) == [("t3", 3)]


def test_notices_reach_the_client(tenants, pooler):
    with pooler.beta() as conn:
        notices = []
        conn.add_notice_handler(lambda diag: notices.append((diag.severity_nonlocalized, diag.sqlstate, diag.message_primary)))
        conn.execute("COMMIT")
        conn.execute("DO $$ BEGIN RAISE NOTICE 'hello %', 7; END $$")
        assert conn.execute("SELECT 'after'").fetchall() == [("after",)]
        assert notices == [("WARNING", "25P01", "there is no transaction in progress"), ("NOTICE", "00000", "hello 7")]
