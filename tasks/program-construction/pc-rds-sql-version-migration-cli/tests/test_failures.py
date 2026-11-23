"""Failed migrations: the dirty flag, what a failed file leaves behind, and refusal until forced.

Adapted from upstream migrate_test.go (TestUpDirty, TestDownDirty, TestStepsDirty, TestForceDirty)
and the reference-preparation probe.
"""

import pytest

from support import fails, migrate, ok, write_migrations

FAILING_UP = {
    "1_widgets.up.sql": "CREATE TABLE widgets (id int);\n",
    "1_widgets.down.sql": "DROP TABLE widgets;\n",
    "2_gadgets.up.sql": "CREATE TABLE gadgets (id int);\nINSERT INTO widgets VALUES (1);\nSELECT no_such_function();\n",
    "2_gadgets.down.sql": "DROP TABLE gadgets;\n",
    "3_gizmos.up.sql": "CREATE TABLE gizmos (id int);\n",
    "3_gizmos.down.sql": "DROP TABLE gizmos;\n",
}


def test_failed_up_records_dirty_version_and_rolls_back_file(db, ws):
    source = write_migrations(ws, FAILING_UP)
    fails(migrate(source, db.url(), "up"))
    assert db.versions() == [(2, True)]
    # The whole file ran as one unit: its earlier statements did not persist.
    assert db.user_tables() == ["widgets"]
    assert db.query("SELECT count(*) FROM widgets") == [(0,)]
    assert ok(migrate(source, db.url(), "version")).stderr.strip() == "2 (dirty)"


@pytest.mark.parametrize("args", [["up"], ["up", "1"], ["down", "1"], ["down", "-all"], ["goto", "1"], ["goto", "3"]])
def test_dirty_database_refuses_to_migrate(db, ws, args):
    source = write_migrations(ws, FAILING_UP)
    fails(migrate(source, db.url(), "up"))
    fails(migrate(source, db.url(), *args))
    assert db.versions() == [(2, True)]
    assert db.user_tables() == ["widgets"]


def test_force_clears_dirty_and_fixed_file_applies(db, ws):
    source = write_migrations(ws, FAILING_UP)
    fails(migrate(source, db.url(), "up"))
    ok(migrate(source, db.url(), "force", "1"))
    assert db.versions() == [(1, False)]
    assert db.user_tables() == ["widgets"]
    write_migrations(ws, {"2_gadgets.up.sql": "CREATE TABLE gadgets (id int);\n"})
    ok(migrate(source, db.url(), "up"))
    assert db.versions() == [(3, False)]
    assert db.user_tables() == ["gadgets", "gizmos", "widgets"]


def test_force_on_dirty_version_itself(db, ws):
    source = write_migrations(ws, FAILING_UP)
    fails(migrate(source, db.url(), "up"))
    ok(migrate(source, db.url(), "force", "2"))
    assert db.versions() == [(2, False)]
    ok(migrate(source, db.url(), "up"))  # 2 is taken as applied; only 3 runs
    assert db.versions() == [(3, False)]
    assert db.user_tables() == ["gizmos", "widgets"]


def test_failed_down_records_target_version_dirty(db, ws):
    files = {
        "1_a.up.sql": "CREATE TABLE a (id int);\n",
        "1_a.down.sql": "DROP TABLE a;\n",
        "3_b.up.sql": "CREATE TABLE b (id int);\n",
        "3_b.down.sql": "DROP TABLE b;\nSELECT 1/0;\n",
        "10_c.up.sql": "CREATE TABLE c (id int);\n",
        "10_c.down.sql": "DROP TABLE c;\n",
    }
    source = write_migrations(ws, files)
    ok(migrate(source, db.url(), "up"))
    fails(migrate(source, db.url(), "down", "2"))
    # 10 was undone; undoing 3 (towards version 1) failed and was rolled back.
    assert db.versions() == [(1, True)]
    assert db.user_tables() == ["a", "b"]
    assert ok(migrate(source, db.url(), "version")).stderr.strip() == "1 (dirty)"


def test_failed_first_down_leaves_dirty_nil_version(db, ws):
    files = {"1_a.up.sql": "CREATE TABLE a (id int);\n", "1_a.down.sql": "DROP TABLE a;\nDROP TABLE nope;\n"}
    source = write_migrations(ws, files)
    ok(migrate(source, db.url(), "up"))
    fails(migrate(source, db.url(), "down", "-all"))
    assert db.versions() == [(-1, True)]
    assert db.user_tables() == ["a"]
    fails(migrate(source, db.url(), "version"))  # no current version to print
    fails(migrate(source, db.url(), "up"))  # but still dirty
    assert db.versions() == [(-1, True)]
    assert db.user_tables() == ["a"]
    ok(migrate(source, db.url(), "force", "--", "-1"))
    assert db.versions() == []
    fails(migrate(source, db.url(), "up"))  # a was never dropped
    assert db.versions() == [(1, True)]


def test_later_migrations_do_not_run_after_failure(db, ws):
    files = {
        "1_a.up.sql": "CREATE TABLE a (id int);\n",
        "2_b.up.sql": "CREATE TABLE b (id int);\nCREATE TABLE b (id int);\n",
        "3_c.up.sql": "CREATE TABLE c (id int);\n",
    }
    source = write_migrations(ws, files)
    fails(migrate(source, db.url(), "goto", "3"))
    assert db.versions() == [(2, True)]
    assert db.user_tables() == ["a"]


def test_lone_statement_runs_outside_transaction_block(db, ws):
    """Upstream examples: CREATE INDEX CONCURRENTLY works when it is the only statement in its file."""
    files = {
        "1_users.up.sql": "CREATE TABLE users (email varchar(40));\n",
        "2_index.up.sql": "CREATE UNIQUE INDEX CONCURRENTLY users_email_index ON users (email);\n\n-- trailing comment\n",
        "3_more.up.sql": "CREATE INDEX CONCURRENTLY users_lower ON users (lower(email));\nCREATE TABLE extra (id int);\n",
    }
    source = write_migrations(ws, files)
    fails(migrate(source, db.url(), "up"))
    assert db.versions() == [(3, True)]
    assert db.user_tables() == ["users"]
    indexes = db.query("SELECT indexname FROM pg_indexes WHERE tablename = 'users' ORDER BY indexname")
    assert indexes == [("users_email_index",)]


def test_drop_works_on_dirty_database(db, ws):
    source = write_migrations(ws, FAILING_UP)
    fails(migrate(source, db.url(), "up"))
    ok(migrate(source, db.url(), "drop", "-f"))
    assert db.user_tables() == []
    assert db.versions() == []
    fails(migrate(source, db.url(), "up"))  # starts over from 1; 2 fails again
    assert db.versions() == [(2, True)]
    assert db.user_tables() == ["widgets"]
