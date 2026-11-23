"""Database URL parameters: bookkeeping table naming, statement splitting and timeouts.

Scenarios adapted from upstream database/postgres/postgres_test.go (testMigrationsTable*, testMultiStatement*,
testStatementTimeout) and database/multistmt/parse_test.go.
"""

import pytest

from support import fails, migrate, ok, write_migrations

PAIR = {
    "1_a.up.sql": "CREATE TABLE a (id int);\n",
    "1_a.down.sql": "DROP TABLE a;\n",
    "2_b.up.sql": "CREATE TABLE b (id int);\n",
    "2_b.down.sql": "DROP TABLE b;\n",
}


@pytest.fixture
def pair(ws):
    return write_migrations(ws, PAIR)


def test_custom_table_name(db, pair):
    url = db.url(**{"x-migrations-table": "deploy_history"})
    ok(migrate(pair, url, "up"))
    assert db.tables() == ["a", "b", "deploy_history"]
    assert db.versions("deploy_history") == [(2, False)]
    assert ok(migrate(pair, url, "version")).stderr.strip() == "2"
    ok(migrate(pair, url, "down", "1"))
    assert db.versions("deploy_history") == [(1, False)]
    assert db.tables() == ["a", "deploy_history"]
    # The default table knows nothing of this history.
    fails(migrate(pair, db.url(), "version"))
    assert db.user_tables() == ["a", "deploy_history"]
    assert db.versions() == []


@pytest.mark.parametrize("name", ["Deploy_History", "deploy.history", '"audit"."h"'])
def test_unquoted_table_name_is_taken_literally(db, pair, name):
    ok(migrate(pair, db.url(**{"x-migrations-table": name}), "up"))
    assert db.tables() == sorted(["a", "b", name])
    assert db.versions(name) == [(2, False)]


def test_quoted_schema_qualified_table(db, pair):
    db.execute("CREATE SCHEMA audit")
    url = db.url(**{"x-migrations-table": '"audit"."history"', "x-migrations-table-quoted": "1"})
    ok(migrate(pair, url, "up"))
    assert db.tables("audit") == ["history"]
    assert db.versions("history", schema="audit") == [(2, False)]
    assert db.tables() == ["a", "b"]
    assert ok(migrate(pair, url, "version")).stderr.strip() == "2"
    same = db.url(**{"x-migrations-table": '"audit"."history"', "x-migrations-table-quoted": "true"})
    ok(migrate(pair, same, "goto", "1"))
    assert db.versions("history", schema="audit") == [(1, False)]
    assert db.tables() == ["a"]


def test_quoted_single_name_keeps_case(db, pair):
    ok(migrate(pair, db.url(**{"x-migrations-table": '"History"', "x-migrations-table-quoted": "1"}), "up"))
    assert db.tables() == ["History", "a", "b"]
    assert db.versions("History") == [(2, False)]


@pytest.mark.parametrize(
    "params",
    [
        {"x-migrations-table": '"missing"."history"', "x-migrations-table-quoted": "1"},  # schema is not created
        {"x-migrations-table": '"a"."b"."c"', "x-migrations-table-quoted": "1"},
        {"x-migrations-table": "history", "x-migrations-table-quoted": "1"},
        {"x-migrations-table": '"history"', "x-migrations-table-quoted": "maybe"},
        {"x-multi-statement": "sure"},
        {"x-statement-timeout": "abc"},
        {"x-multi-statement": "true", "x-multi-statement-max-size": "abc"},
    ],
)
def test_invalid_parameters_touch_nothing(db, pair, params):
    fails(migrate(pair, db.url(**params), "up"))
    # Nothing ran; an empty bookkeeping table would be equivalent to none.
    assert db.user_tables() == []
    assert db.versions() == []
    assert db.query("SELECT nspname FROM pg_namespace WHERE nspname IN ('missing', 'a')") == []


def test_independent_histories_in_one_database(db, ws):
    first = write_migrations(ws, {"1_a.up.sql": "CREATE TABLE ta1 (id int);\n", "2_a.up.sql": "CREATE TABLE ta2 (id int);\n", "2_a.down.sql": "DROP TABLE ta2;\n"}, "first")
    second = write_migrations(ws, {"5_b.up.sql": "CREATE TABLE tb5 (id int);\n", "5_b.down.sql": "DROP TABLE tb5;\n"}, "second")
    ok(migrate(first, db.url(**{"x-migrations-table": "hist_a"}), "up"))
    ok(migrate(second, db.url(**{"x-migrations-table": "hist_b"}), "up"))
    assert db.versions("hist_a") == [(2, False)]
    assert db.versions("hist_b") == [(5, False)]
    ok(migrate(first, db.url(**{"x-migrations-table": "hist_a"}), "down", "1"))
    assert db.versions("hist_a") == [(1, False)]
    assert db.versions("hist_b") == [(5, False)]
    assert db.tables() == ["hist_a", "hist_b", "ta1", "tb5"]


def test_current_schema_from_role_setting(db, pair):
    """The session's current schema (here a server-side role default) decides where history and drop act."""
    db.execute("CREATE SCHEMA tenant; CREATE TABLE public.keep (id int)")
    db.set_role_search_path("app", "tenant")
    ok(migrate(pair, db.url(), "up"))
    assert db.tables("tenant") == ["a", "b", "schema_migrations"]
    assert db.versions(schema="tenant") == [(2, False)]
    assert db.tables() == ["keep"]
    assert ok(migrate(pair, db.url(), "version")).stderr.strip() == "2"
    ok(migrate(pair, db.url(), "down", "1"))
    assert db.versions(schema="tenant") == [(1, False)]
    assert db.tables("tenant") == ["a", "schema_migrations"]
    ok(migrate(pair, db.url(), "drop", "-f"))
    assert db.user_tables(schema="tenant") == []
    assert db.versions(schema="tenant") == []
    assert db.tables() == ["keep"]


def test_current_schema_with_custom_table(db, pair):
    db.execute("CREATE SCHEMA tenant")
    db.set_role_search_path("app", "tenant")
    ok(migrate(pair, db.url(**{"x-migrations-table": "hist"}), "up"))
    assert db.tables("tenant") == ["a", "b", "hist"]
    assert db.versions("hist", schema="tenant") == [(2, False)]
    assert db.tables() == []


def test_postgresql_scheme(db, pair):
    ok(migrate(pair, db.url(scheme="postgresql"), "up"))
    assert db.versions() == [(2, False)]


def test_wrong_password_fails(db, pair):
    fails(migrate(pair, db.url(password="not-the-password"), "up"))
    assert db.tables() == []


SPLIT = {"1_a.up.sql": "CREATE TABLE m1 (id int); CREATE TABLE m2 (id int);\nSELECT 1/0;\nCREATE TABLE m3 (id int);\n"}


def test_whole_file_is_one_unit_by_default(db, ws):
    source = write_migrations(ws, SPLIT)
    fails(migrate(source, db.url(), "up"))
    assert db.versions() == [(1, True)]
    assert db.user_tables() == []


def test_multi_statement_runs_statements_separately(db, ws):
    source = write_migrations(ws, SPLIT)
    fails(migrate(source, db.url(**{"x-multi-statement": "true"}), "up"))
    assert db.versions() == [(1, True)]
    assert db.user_tables() == ["m1", "m2"]


def test_multi_statement_splits_on_every_semicolon(db, ws):
    source = write_migrations(ws, {"1_a.up.sql": "CREATE TABLE t (v text);\nINSERT INTO t VALUES ('a;b');\n"})
    fails(migrate(source, db.url(**{"x-multi-statement": "1"}), "up"))
    assert db.versions() == [(1, True)]
    assert db.user_tables() == ["t"]
    assert db.query("SELECT v FROM t") == []


def test_semicolon_in_literal_without_splitting(db, ws):
    source = write_migrations(ws, {"1_a.up.sql": "CREATE TABLE t (v text);\nINSERT INTO t VALUES ('a;b');\n"})
    ok(migrate(source, db.url(**{"x-multi-statement": "false"}), "up"))
    assert db.versions() == [(1, False)]
    assert db.query("SELECT v FROM t") == [("a;b",)]


def test_multi_statement_allows_non_transactional_statement(db, ws):
    files = {
        "1_users.up.sql": "CREATE TABLE users (email text);\n",
        "2_idx.up.sql": "CREATE INDEX CONCURRENTLY users_e ON users (email);\nCREATE TABLE extra (id int);\n",
    }
    source = write_migrations(ws, files)
    ok(migrate(source, db.url(**{"x-multi-statement": "true"}), "up"))
    assert db.versions() == [(2, False)]
    assert db.user_tables() == ["extra", "users"]
    assert db.query("SELECT indexname FROM pg_indexes WHERE tablename = 'users'") == [("users_e",)]


BIG = {"1_a.up.sql": "CREATE TABLE s1 (v text);\nINSERT INTO s1 VALUES ('" + "x" * 6000 + "');\nCREATE TABLE s3 (id int);\n"}


@pytest.mark.parametrize(("size", "accepted"), [("5000", False), ("7000", True), (None, True)])
def test_multi_statement_max_size(db, ws, size, accepted):
    source = write_migrations(ws, BIG)
    params = {"x-multi-statement": "true"} | ({"x-multi-statement-max-size": size} if size else {})
    result = migrate(source, db.url(**params), "up")
    if accepted:
        ok(result)
        assert db.versions() == [(1, False)]
        assert db.user_tables() == ["s1", "s3"]
        assert db.query("SELECT length(v) FROM s1") == [(6000,)]
    else:
        fails(result)
        assert db.versions() == [(1, True)]
        assert db.user_tables() == ["s1"]  # the statement before the oversized one ran
        assert db.query("SELECT count(*) FROM s1") == [(0,)]


def test_max_size_ignored_without_splitting(db, ws):
    source = write_migrations(ws, BIG)
    ok(migrate(source, db.url(**{"x-multi-statement-max-size": "100"}), "up"))
    assert db.versions() == [(1, False)]
    assert db.user_tables() == ["s1", "s3"]


SLOW = {
    "1_fast.up.sql": "CREATE TABLE fast (id int);\n",
    "2_slow.up.sql": "SELECT pg_sleep(0.5);\nSELECT pg_sleep(0.5);\nSELECT pg_sleep(0.5);\nSELECT pg_sleep(0.5);\nSELECT pg_sleep(0.5);\n",
    "3_after.up.sql": "CREATE TABLE after_slow (id int);\n",
}


def test_statement_timeout_applies_to_whole_file(db, ws):
    source = write_migrations(ws, SLOW)
    fails(migrate(source, db.url(**{"x-statement-timeout": "1500"}), "up"))
    assert db.versions() == [(2, True)]
    assert db.user_tables() == ["fast"]


def test_statement_timeout_applies_per_split_statement(db, ws):
    source = write_migrations(ws, SLOW)
    ok(migrate(source, db.url(**{"x-statement-timeout": "1500", "x-multi-statement": "true"}), "up"))
    assert db.versions() == [(3, False)]
    assert db.user_tables() == ["after_slow", "fast"]


def test_no_timeout_by_default(db, ws):
    source = write_migrations(ws, SLOW)
    ok(migrate(source, db.url(), "up"))
    assert db.versions() == [(3, False)]
