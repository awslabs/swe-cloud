"""Multi-step sequences: gaps in the version list, recovery after failures, and concurrent invocations."""

import time
from pathlib import Path

from harness import start_command

from support import fails, journal, migrate, ok, write_migrations

# Upstream migrate_test.go sourceStubMigrations:  1: up+down, 3: up, 4: up+down, 5: down, 7: up+down.
GAPS = {
    "1_one.up.sql": journal("1u"),
    "1_one.down.sql": journal("1d"),
    "3_three.up.sql": journal("3u"),
    "4_four.up.sql": journal("4u"),
    "4_four.down.sql": journal("4d"),
    "5_five.down.sql": journal("5d"),
    "7_seven.up.sql": journal("7u"),
    "7_seven.down.sql": journal("7d"),
}


def test_versions_with_missing_up_or_down_files(db, ws):
    db.add_journal()
    source = write_migrations(ws, GAPS)
    steps = [
        (["goto", "5"], [(5, False)], ["1u", "3u", "4u"]),
        (["up"], [(7, False)], ["7u"]),
        (["goto", "3"], [(3, False)], ["7d", "5d", "4d"]),
        (["down", "1"], [(1, False)], []),  # 3 has no down file
        (["down", "1"], [], ["1d"]),
        (["up", "4"], [(5, False)], ["1u", "3u", "4u"]),  # 5 counts as a step with no up file
        (["down", "-all"], [], ["5d", "4d", "1d"]),
    ]
    ran = []
    for args, version, new in steps:
        ok(migrate(source, db.url(), *args))
        ran += new
        assert db.versions() == version, args
        assert db.journal() == ran, args
    for missing in ("2", "6", "8"):
        fails(migrate(source, db.url(), "goto", missing))
        assert db.versions() == []
        assert db.journal() == ran


def test_upstream_example_migrations_from_s3(db, bucket):
    examples = Path(__file__).parent / "fixtures" / "examples"
    bucket.put({path.name: path.read_text() for path in sorted(examples.iterdir())}, prefix="svc/")
    source = bucket.url("svc")

    ok(migrate(source, db.url(), "up", "3"))
    assert db.versions() == [(1285849751, False)]
    assert db.user_tables() == ["users"]
    columns = db.query("SELECT column_name FROM information_schema.columns WHERE table_name = 'users' ORDER BY ordinal_position")
    assert columns == [("user_id",), ("name",), ("email",), ("city",)]
    indexes = db.query("SELECT indexname FROM pg_indexes WHERE tablename = 'users' ORDER BY indexname")
    assert indexes == [("users_email_index",), ("users_user_id_key",)]

    ok(migrate(source, db.url(), "up"))
    assert db.versions() == [(1885849751, False)]
    assert db.user_tables() == ["books", "movies", "users"]
    ok(migrate(source, db.url(), "down", "4"))  # four comment-only versions without down files
    assert db.versions() == [(1485949617, False)]
    assert db.user_tables() == ["books", "movies", "users"]
    ok(migrate(source, db.url(), "goto", "1185749658"))
    assert db.versions() == [(1185749658, False)]
    assert db.user_tables() == ["users"]
    assert db.query("SELECT indexname FROM pg_indexes WHERE tablename = 'users'") == [("users_user_id_key",)]
    ok(migrate(source, db.url(), "down", "-all"))
    assert db.versions() == []
    assert db.user_tables() == []


def test_dirty_recovery_from_s3(db, bucket):
    db.add_journal()
    bucket.put(
        {
            "1_a.up.sql": journal("1u"),
            "2_b.up.sql": journal("2u") + "SELECT 1/0;\n",
            "3_c.up.sql": journal("3u"),
            "3_c.down.sql": journal("3d"),
        },
        prefix="release/",
    )
    source = bucket.url("release")
    fails(migrate(source, db.url(), "up"))
    assert db.versions() == [(2, True)]
    assert db.journal() == ["1u"]
    fails(migrate(source, db.url(), "up"))
    assert db.versions() == [(2, True)]
    assert ok(migrate(source, db.url(), "version")).stderr.strip() == "2 (dirty)"

    bucket.put({"2_b.up.sql": journal("2u-fixed"), "4_d.up.sql": journal("4u")}, prefix="release/")
    ok(migrate(source, db.url(), "force", "1"))
    assert db.versions() == [(1, False)]
    ok(migrate(source, db.url(), "up", "2"))
    assert db.versions() == [(3, False)]
    assert db.journal() == ["1u", "2u-fixed", "3u"]
    ok(migrate(source, db.url(), "up"))
    assert db.versions() == [(4, False)]
    ok(migrate(source, db.url(), "goto", "2"))
    assert db.versions() == [(2, False)]
    assert db.journal() == ["1u", "2u-fixed", "3u", "4u", "3d"]


def test_forced_version_without_files_blocks_until_forced_back(db, ws):
    db.add_journal()
    source = write_migrations(ws, {"1_a.up.sql": journal("1u"), "1_a.down.sql": journal("1d"), "3_b.up.sql": journal("3u")})
    ok(migrate(source, db.url(), "up", "1"))
    ok(migrate(source, db.url(), "force", "7"))
    assert db.versions() == [(7, False)]
    assert ok(migrate(source, db.url(), "version")).stderr.strip() == "7"
    for args in (["up"], ["down", "1"], ["down", "-all"], ["goto", "3"], ["goto", "1"]):
        fails(migrate(source, db.url(), *args))
        assert db.versions() == [(7, False)], args
    assert db.journal() == ["1u"]
    # The file for 7 appears later: now 7 is a known version.
    write_migrations(ws, {"7_c.down.sql": journal("7d")})
    ok(migrate(source, db.url(), "goto", "1"))
    assert db.versions() == [(1, False)]
    assert db.journal() == ["1u", "7d"]  # 3 has no down file


def test_source_and_database_switch_between_runs(db, bucket, ws):
    db.add_journal()
    bucket.put({"p/1_a.up.sql": journal("1u-s3"), "p/2_b.up.sql": journal("2u-s3"), "p/2_b.down.sql": journal("2d-s3")})
    local = write_migrations(
        ws, {"1_a.up.sql": journal("1u-file"), "2_b.up.sql": journal("2u-file"), "2_b.down.sql": journal("2d-file"), "3_c.up.sql": journal("3u-file")}
    )
    ok(migrate(bucket.url("p"), db.url(), "up"))
    ok(migrate(local, db.url(), "up"))
    assert db.versions() == [(3, False)]
    assert ok(migrate(bucket.url("p"), db.url(), "version")).stderr.strip() == "3"
    fails(migrate(bucket.url("p"), db.url(), "down", "1"))  # version 3 is unknown to the S3 prefix
    assert db.versions() == [(3, False)]
    ok(migrate(local, db.url(), "down", "1"))
    ok(migrate(bucket.url("p"), db.url(), "down", "1"))
    assert db.versions() == [(1, False)]
    assert db.journal() == ["1u-s3", "2u-s3", "3u-file", "2d-s3"]


SLOW_RELEASE = {"1_a.up.sql": journal("1u"), "2_slow.up.sql": journal("2u") + "SELECT pg_sleep(4);\n", "3_c.up.sql": journal("3u")}


def start_migrate(source, url, *args):
    return start_command(["/app/program", "-path", str(source), "-database", url, *args], stdout=-1, stderr=-1, text=True)


def wait_until_running(db, process, schema="public"):
    """Wait until the first invocation is inside its slow migration."""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if db.journal() == ["1u"] and db.versions(schema=schema) == [(2, True)]:
            return
        assert process.poll() is None, process.communicate()
        time.sleep(0.05)
    raise AssertionError("first invocation never reached its slow migration")


def stop(process):
    if process.poll() is None:
        process.kill()
    return process.communicate(timeout=30)


def test_concurrent_runs_on_same_history_are_serialized(db, ws):
    db.add_journal()
    source = write_migrations(ws, SLOW_RELEASE)
    first = start_migrate(source, db.url(), "up")
    try:
        wait_until_running(db, first)
        ok(migrate(source, db.url(), "up", timeout=40))
        # The second run waited: by the time it finished, the first had completed all of its work.
        assert db.journal() == ["1u", "2u", "3u"]
        _, err = first.communicate(timeout=30)
        assert first.returncode == 0, err
    finally:
        stop(first)
    assert db.versions() == [(3, False)]
    assert db.journal() == ["1u", "2u", "3u"]


def test_version_waits_for_running_migration(db, ws):
    db.add_journal()
    source = write_migrations(ws, SLOW_RELEASE)
    first = start_migrate(source, db.url(), "up")
    try:
        wait_until_running(db, first)
        assert ok(migrate(source, db.url(), "version", timeout=40)).stderr.strip() == "3"
    finally:
        stop(first)


def test_other_history_is_not_blocked(db, ws, engine):
    db.add_journal()
    source = write_migrations(ws, SLOW_RELEASE)
    other = write_migrations(ws, {"7_b.up.sql": "CREATE TABLE tb (id int);\n"}, "other")
    elsewhere = engine.create_database()
    first = start_migrate(source, db.url(), "up")
    try:
        wait_until_running(db, first)
        ok(migrate(other, db.url(**{"x-migrations-table": "hist_b"}), "up"))
        assert first.poll() is None, "a different bookkeeping table waited for the running migration"
        ok(migrate(other, elsewhere.url(), "up"))
        assert first.poll() is None, "a different database waited for the running migration"
        assert db.versions("hist_b") == [(7, False)]
        assert elsewhere.versions() == [(7, False)]
        _, err = first.communicate(timeout=30)
        assert first.returncode == 0, err
    finally:
        stop(first)
        engine.drop_database(elsewhere.name)
    assert db.versions() == [(3, False)]


def test_other_schema_is_not_blocked(db, ws, schema_role):
    """Same database and table name, different current schema (per-role server setting)."""
    role, password = schema_role
    db.add_journal()
    db.execute(
        f'CREATE SCHEMA s1; CREATE SCHEMA s2 AUTHORIZATION "{role}"; GRANT USAGE ON SCHEMA public TO "{role}"; '
        f'GRANT ALL ON public.journal, public.journal_seq_seq TO "{role}"'
    )
    db.set_role_search_path("app", "s1")
    db.set_role_search_path(role, "s2")
    source = write_migrations(ws, SLOW_RELEASE)
    other = write_migrations(ws, {"5_x.up.sql": journal("5u") + "CREATE TABLE x (id int);\n"}, "other")
    as_role = db.url(user=role, password=password)
    first = start_migrate(source, db.url(), "up")
    try:
        wait_until_running(db, first, schema="s1")
        ok(migrate(other, as_role, "up"))
        assert first.poll() is None, "a different schema waited for the running migration"
        assert db.versions(schema="s2") == [(5, False)]
        _, err = first.communicate(timeout=30)
        assert first.returncode == 0, err
    finally:
        stop(first)
    assert db.versions(schema="s1") == [(3, False)]
    assert db.tables("s2") == ["schema_migrations", "x"]
    assert db.journal() == ["1u", "2u", "5u", "3u"]
    ok(migrate(other, as_role, "drop", "-f"))
    assert db.user_tables(schema="s2") == []
    assert db.versions(schema="s2") == []
    assert db.tables("s1") == ["schema_migrations"]
    assert db.versions(schema="s1") == [(3, False)]
    assert db.tables() == ["journal"]
