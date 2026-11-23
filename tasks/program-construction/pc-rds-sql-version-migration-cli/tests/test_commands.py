"""Individual commands against a local migration directory; each checks the recorded version and tables.

Step semantics adapted from upstream migrate_test.go (TestSteps, TestUpAndDown, TestMigrate, TestForce, TestVersion).
"""

import pytest
from harness import run_program

from support import fails, migrate, ok, write_migrations

THREE = {
    "1_a.up.sql": "CREATE TABLE a (id int);\n",
    "1_a.down.sql": "DROP TABLE a;\n",
    "3_b.up.sql": "CREATE TABLE b (id int);\n",
    "3_b.down.sql": "DROP TABLE b;\n",
    "10_c.up.sql": "CREATE TABLE c (id int);\n",
    "10_c.down.sql": "DROP TABLE c;\n",
}


@pytest.fixture
def three(ws):
    return write_migrations(ws, THREE)


def at(db, three, version):
    """Bring the database to a version through the program itself."""
    ok(migrate(three, db.url(), "goto", str(version)))


def test_up_applies_everything_in_numeric_order(db, three):
    ok(migrate(three, db.url(), "up"))
    assert db.versions() == [(10, False)]
    assert db.user_tables() == ["a", "b", "c"]
    columns = db.query(
        "SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'schema_migrations' ORDER BY column_name"
    )
    assert columns == [("dirty", "boolean"), ("version", "bigint")]


def test_up_with_limit_and_short_limit(db, three):
    ok(migrate(three, db.url(), "up", "2"))
    assert db.versions() == [(3, False)]
    assert db.user_tables() == ["a", "b"]
    # Only one migration remains: it is applied, then the shortfall is an error.
    fails(migrate(three, db.url(), "up", "5"))
    assert db.versions() == [(10, False)]
    assert db.user_tables() == ["a", "b", "c"]


def test_up_when_nothing_is_pending(db, three):
    ok(migrate(three, db.url(), "up"))
    ok(migrate(three, db.url(), "up"))  # no change is success
    assert db.versions() == [(10, False)]
    fails(migrate(three, db.url(), "up", "1"))  # an explicit step that cannot be taken is not
    assert db.versions() == [(10, False)]
    ok(migrate(three, db.url(), "up", "0"))
    assert db.versions() == [(10, False)]
    assert db.user_tables() == ["a", "b", "c"]


def test_down_with_limit(db, three):
    at(db, three, 10)
    ok(migrate(three, db.url(), "down", "2"))
    assert db.versions() == [(1, False)]
    assert db.user_tables() == ["a"]
    ok(migrate(three, db.url(), "down", "1"))
    assert db.versions() == []
    assert db.user_tables() == []


def test_down_short_limit_applies_what_exists(db, three):
    at(db, three, 3)
    fails(migrate(three, db.url(), "down", "5"))
    assert db.versions() == []
    assert db.user_tables() == []


def test_down_zero_and_nil_version(db, three):
    at(db, three, 3)
    ok(migrate(three, db.url(), "down", "0"))
    assert db.versions() == [(3, False)]
    ok(migrate(three, db.url(), "down", "-all"))
    assert db.versions() == []
    ok(migrate(three, db.url(), "down", "-all"))  # nothing to undo: no change
    fails(migrate(three, db.url(), "down", "1"))
    assert db.versions() == []
    assert db.user_tables() == []


@pytest.mark.parametrize(("answer", "applied"), [("n\n", False), (None, False), ("yes\n", False), (" Y \n", True), ("y\n", True)])
def test_down_without_argument_asks_first(db, three, answer, applied):
    at(db, three, 10)
    result = migrate(three, db.url(), "down", stdin=answer)
    if applied:
        ok(result)
        assert db.versions() == []
        assert db.user_tables() == []
    else:
        fails(result)
        assert db.versions() == [(10, False)]
        assert db.user_tables() == ["a", "b", "c"]


def test_goto_moves_up_and_down(db, three):
    ok(migrate(three, db.url(), "goto", "3"))
    assert db.versions() == [(3, False)]
    assert db.user_tables() == ["a", "b"]
    ok(migrate(three, db.url(), "goto", "3"))
    assert db.versions() == [(3, False)]
    ok(migrate(three, db.url(), "goto", "10"))
    assert db.versions() == [(10, False)]
    ok(migrate(three, db.url(), "goto", "1"))
    assert db.versions() == [(1, False)]
    assert db.user_tables() == ["a"]


@pytest.mark.parametrize("target", ["2", "0", "11"])
def test_goto_unknown_version_changes_nothing(db, three, target):
    at(db, three, 3)
    fails(migrate(three, db.url(), "goto", target))
    assert db.versions() == [(3, False)]
    assert db.user_tables() == ["a", "b"]


def test_version_reports(db, three):
    nil = migrate(three, db.url(), "version")
    fails(nil)
    at(db, three, 3)
    assert ok(migrate(three, db.url(), "version")).stderr.strip() == "3"
    db.execute("UPDATE schema_migrations SET dirty = true")
    assert ok(migrate(three, db.url(), "version")).stderr.strip() == "3 (dirty)"
    assert db.versions() == [(3, True)]


def test_force_sets_version_without_running(db, three):
    ok(migrate(three, db.url(), "force", "3"))
    assert db.versions() == [(3, False)]
    assert db.user_tables() == []
    assert ok(migrate(three, db.url(), "version")).stderr.strip() == "3"
    ok(migrate(three, db.url(), "up"))  # continues after the forced version
    assert db.versions() == [(10, False)]
    assert db.user_tables() == ["c"]


def test_force_nil_version(db, three):
    at(db, three, 3)
    ok(migrate(three, db.url(), "force", "--", "-1"))
    assert db.versions() == []
    assert db.user_tables() == ["a", "b"]
    fails(migrate(three, db.url(), "version"))
    # With no version recorded, up starts again from the first file, whose table already exists.
    fails(migrate(three, db.url(), "up"))
    assert db.versions() == [(1, True)]
    assert db.user_tables() == ["a", "b"]


@pytest.mark.parametrize("args", [["force", "-1"], ["force", "--", "-2"], ["force", "x"], ["force", "3.5"], ["force"]])
def test_force_rejects_bad_arguments(db, three, args):
    at(db, three, 3)
    fails(migrate(three, db.url(), *args))
    assert db.versions() == [(3, False)]
    assert db.user_tables() == ["a", "b"]


@pytest.mark.parametrize(
    "args",
    [
        ["up", "abc"],
        ["up", "--", "-1"],
        ["up", "-bogus"],
        ["down", "x"],
        ["down", "1", "-all"],
        ["down", "-all", "1"],
        ["down", "1", "2"],
        ["goto"],
        ["goto", "v3"],
        ["goto", "--", "-1"],
        ["sideways"],
    ],
)
def test_bad_arguments_change_nothing(db, three, args):
    at(db, three, 3)
    fails(migrate(three, db.url(), *args))
    assert db.versions() == [(3, False)]
    assert db.user_tables() == ["a", "b"]


@pytest.mark.parametrize("answer", [None, "n\n", "yes\n"])
def test_drop_declined(db, three, answer):
    at(db, three, 10)
    fails(migrate(three, db.url(), "drop", stdin=answer))
    assert db.versions() == [(10, False)]
    assert db.user_tables() == ["a", "b", "c"]


@pytest.mark.parametrize("args", [["drop", "-f"], ["drop"]])
def test_drop_removes_tables(db, three, args):
    at(db, three, 10)
    ok(migrate(three, db.url(), *args, stdin="y\n"))
    assert db.user_tables() == []
    assert db.versions() == []
    fails(migrate(three, db.url(), "version"))
    ok(migrate(three, db.url(), "up"))
    assert db.versions() == [(10, False)]
    assert db.user_tables() == ["a", "b", "c"]


def test_drop_scope(db, three):
    db.execute(
        "CREATE SCHEMA other; CREATE TABLE other.keep (id int); CREATE SEQUENCE lone_seq; CREATE TYPE mood AS ENUM ('ok')"
    )
    at(db, three, 10)
    db.execute("CREATE VIEW va AS SELECT * FROM a")
    ok(migrate(three, db.url(), "drop", "-f"))
    assert db.user_tables() == []
    assert db.versions() == []
    assert db.tables("other") == ["keep"]
    relations = db.query("SELECT relname, relkind::text FROM pg_class WHERE relnamespace = 'public'::regnamespace AND relname NOT LIKE 'schema_migrations%%'")
    assert relations == [("lone_seq", "S")]
    assert db.query("SELECT typname FROM pg_type WHERE typnamespace = 'public'::regnamespace AND typtype = 'e'") == [("mood",)]


def test_global_options_accepted(db, three):
    options = ["-verbose", "-prefetch", "1", "-lock-timeout", "5", "-path", str(three), "-database", db.url()]
    ok(run_program([*options, "up", "2"]))
    assert db.versions() == [(3, False)]
