"""How local directories and S3 prefixes become one ordered set of versions.

Adapted from upstream source/parse_test.go, source/migration_test.go, source/aws_s3/s3_test.go
(Test, TestLoadMigrationsPaginates, TestParseURI) and the reference-preparation probe.
"""

import pytest
from harness import give_to_candidate, run_program, write_fixture

from support import fails, journal, migrate, ok, write_migrations


def test_file_names_and_numeric_order(db, ws):
    db.add_journal()
    files = {
        "001_a.up.sql": journal("1u"),
        "9_b.up.sql": journal("9u"),
        "10_c.up.sql": journal("10u"),
        "12_x.y.up.sql": journal("12u"),
        "13_.up.sql": journal("13u"),
        "README.md": "not a migration",
        "notes.up.sql": "SELECT broken",
        "2_x.sql": "SELECT broken",
        "_3.up.sql": "SELECT broken",
        "4_x.UP.sql": "SELECT broken",
        "5_x.sideways.sql": "SELECT broken",
    }
    source = write_migrations(ws, files)
    (source / "20_dir.up.sql").mkdir()
    give_to_candidate(source / "20_dir.up.sql")
    write_fixture(source / "sub" / "14_nested.up.sql", "SELECT broken")
    ok(migrate(source, db.url(), "up"))
    assert db.versions() == [(13, False)]
    assert db.journal() == ["1u", "9u", "10u", "12u", "13u"]


def test_file_url_with_absolute_path(db, ws):
    db.add_journal()
    source = write_migrations(ws, {"1_a.up.sql": journal("1u"), "2_b.up.sql": journal("2u")})
    ok(migrate(f"file://{source}", db.url(), "up"))
    assert db.versions() == [(2, False)]
    assert db.journal() == ["1u", "2u"]


def test_source_option_wins_over_path(db, ws):
    db.add_journal()
    chosen = write_migrations(ws, {"1_a.up.sql": journal("1u-source"), "2_b.up.sql": journal("2u-source")}, "chosen")
    ignored = write_migrations(ws, {"1_a.up.sql": journal("1u-path"), "5_c.up.sql": journal("5u-path")}, "ignored")
    ok(run_program(["-path", str(ignored), "-source", f"file://{chosen}", "-database", db.url(), "up"]))
    assert db.versions() == [(2, False)]
    assert db.journal() == ["1u-source", "2u-source"]


def test_up_and_down_files_may_have_different_titles(db, ws):
    db.add_journal()
    source = write_migrations(ws, {"1_create.up.sql": journal("1u"), "1_remove.down.sql": journal("1d"), "2_c.up.sql": journal("2u")})
    ok(migrate(source, db.url(), "up"))
    ok(migrate(source, db.url(), "down", "-all"))
    assert db.versions() == []
    assert db.journal() == ["1u", "2u", "1d"]


@pytest.mark.parametrize("names", [["1_a.up.sql", "001_b.up.sql"], ["7_a.down.sql", "07_b.down.sql", "7_a.up.sql"]])
def test_duplicate_version_rejected_before_running(db, ws, names):
    db.add_journal()
    source = write_migrations(ws, {name: journal(name) for name in names})
    fails(migrate(source, db.url(), "up"))
    assert db.versions() == []
    assert db.journal() == []


def test_empty_and_comment_only_files_still_advance(db, ws):
    db.add_journal()
    source = write_migrations(ws, {"1_e.up.sql": "", "2_c.up.sql": "-- only a comment\n", "3_x.up.sql": journal("3u")})
    ok(migrate(source, db.url(), "up"))
    assert db.versions() == [(3, False)]
    assert db.journal() == ["3u"]


def test_empty_directory(db, ws):
    source = write_migrations(ws, {})
    fails(migrate(source, db.url(), "up"))
    assert db.versions() == []
    ok(migrate(source, db.url(), "down", "-all"))
    fails(migrate(source, db.url(), "version"))


def test_missing_directory(db, ws):
    fails(migrate(ws / "absent", db.url(), "up"))
    assert db.versions() == []
    assert db.user_tables() == []


S3_LAYOUT = {
    "rel/v1/1_a.up.sql": journal("1u"),
    "rel/v1/1_a.down.sql": journal("1d"),
    "rel/v1/2_b.up.sql": journal("2u"),
    "rel/v1/notes.txt": "not a migration",
    "rel/v1/x/3_c.up.sql": journal("3u-nested"),
    "rel/v10/3_d.up.sql": journal("3u-sibling"),
    "rel/v13_e.up.sql": journal("13u-adjacent"),
    "rel/v1README": "x",
    "5_root.up.sql": journal("5u-root"),
}


@pytest.mark.parametrize("prefix", ["rel/v1", "rel/v1/", "/rel/v1//"])
def test_s3_prefix_selects_direct_children(db, bucket, prefix):
    db.add_journal()
    bucket.put(S3_LAYOUT)
    ok(migrate(bucket.url(prefix), db.url(), "up"))
    assert db.versions() == [(2, False)]
    assert db.journal() == ["1u", "2u"]
    ok(migrate(bucket.url(prefix), db.url(), "down", "-all"))
    assert db.versions() == []
    assert db.journal() == ["1u", "2u", "1d"]
    assert bucket.keys() == sorted(S3_LAYOUT)


@pytest.mark.parametrize("trailing", ["", "/"])
def test_s3_bucket_root(db, bucket, trailing):
    db.add_journal()
    bucket.put(S3_LAYOUT)
    ok(migrate(bucket.url() + trailing, db.url(), "up"))
    assert db.versions() == [(5, False)]
    assert db.journal() == ["5u-root"]


def test_s3_empty_prefix(db, bucket):
    bucket.put(S3_LAYOUT)
    fails(migrate(bucket.url("nothing"), db.url(), "up"))
    assert db.versions() == []
    fails(migrate(bucket.url("nothing"), db.url(), "version"))
    ok(migrate(bucket.url("nothing"), db.url(), "down", "-all"))
    assert db.user_tables() == []


def test_s3_missing_bucket(db, bucket):
    db.add_journal()
    fails(migrate(f"s3://{bucket.name}-absent/rel", db.url(), "up"))
    assert db.versions() == []
    assert db.user_tables() == ["journal"]


def test_s3_duplicate_version(db, bucket):
    db.add_journal()
    bucket.put({"p/1_a.up.sql": journal("1u"), "p/01_b.up.sql": journal("1u-b"), "p/2_c.up.sql": journal("2u")})
    fails(migrate(bucket.url("p"), db.url(), "up"))
    assert db.versions() == []
    assert db.journal() == []


def test_s3_listing_past_one_page(db, bucket):
    db.add_journal()
    bucket.put({"p/1_first.up.sql": journal("1u"), "p/7_last.up.sql": journal("7u"), "p/7_last.down.sql": journal("7d")})
    # 1105 non-migration keys sort between the two migrations, pushing 7 beyond the first listing page.
    bucket.put_many({f"p/5-note-{index:04d}.txt": "x" for index in range(1105)})
    ok(migrate(bucket.url("p"), db.url(), "up"))
    assert db.versions() == [(7, False)]
    assert db.journal() == ["1u", "7u"]
    ok(migrate(bucket.url("p"), db.url(), "down", "1"))
    assert db.versions() == [(1, False)]
    assert db.journal() == ["1u", "7u", "7d"]

