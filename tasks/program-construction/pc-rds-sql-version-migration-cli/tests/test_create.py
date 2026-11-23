"""`create`: naming of new migration pairs; needs no database.

Adapted from upstream internal/cli/commands_test.go (TestCreateCmdSuite, nextSeqVersion cases).
"""

import os
import stat
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from harness import run_program, write_fixture

from support import fails, ok


def listing(directory):
    """Names of regular, empty files directly in a candidate-written directory (never follows links)."""
    names = sorted(os.listdir(directory))
    for name in names:
        info = os.lstat(directory / name)
        assert stat.S_ISREG(info.st_mode), f"{name} is not a regular file"
        assert info.st_size == 0, f"{name} is not empty"
    return names


def printed(result):
    """Non-blank stderr lines, order ignored, repetitions kept."""
    return sorted(line for line in result.stderr.splitlines() if line.strip())


def create(ws, *args):
    return run_program(["create", *args], cwd=ws)


def test_sequential_pairs_in_new_nested_directory(ws):
    first = ok(create(ws, "-ext", "sql", "-dir", "db/migrations", "-seq", "init"))
    target = ws / "db" / "migrations"
    assert listing(target) == ["000001_init.down.sql", "000001_init.up.sql"]
    assert printed(first) == sorted([str(target / "000001_init.up.sql"), str(target / "000001_init.down.sql")])

    second = ok(create(ws, "-ext", ".sql", "-dir", "db/migrations", "-seq", "add_users"))
    assert printed(second) == sorted([str(target / "000002_add_users.up.sql"), str(target / "000002_add_users.down.sql")])
    ok(create(ws, "-ext", "sql", "-dir", "db/migrations", "-seq", "-digits", "2", "third"))
    assert listing(target) == [
        "000001_init.down.sql",
        "000001_init.up.sql",
        "000002_add_users.down.sql",
        "000002_add_users.up.sql",
        "03_third.down.sql",
        "03_third.up.sql",
    ]


def test_sequential_default_directory_is_working_directory(ws):
    ok(create(ws, "-ext", "sql", "-seq", "-digits", "1", "x"))
    result = ok(create(ws, "-ext", "sql", "-seq", "-digits", "1", "y"))
    assert listing(ws) == ["1_x.down.sql", "1_x.up.sql", "2_y.down.sql", "2_y.up.sql"]
    assert printed(result) == sorted([str(ws / "2_y.up.sql"), str(ws / "2_y.down.sql")])


@pytest.mark.parametrize(
    ("existing", "created"),
    [
        (["9_a.up.sql", "10_b.up.sql"], "000010_next"),  # the name that sorts last, not the largest number
        (["README.md", "7_x.up.sql"], "000008_next"),  # only files with the extension count
        (["0003_three.sql", "0004_four.sql"], "000005_next"),
        (["000001_a.up.sql", "2_b.up.sql"], "000003_next"),
    ],
)
def test_sequential_number_follows_last_listed_file(ws, existing, created):
    target = ws / "mig"
    for name in existing:
        write_fixture(target / name, "")
    ok(create(ws, "-ext", "sql", "-seq", "-dir", "mig", "next"))
    assert listing(target) == sorted([*existing, f"{created}.up.sql", f"{created}.down.sql"])


@pytest.mark.parametrize(
    ("existing", "args"),
    [
        (["9_nine.up.sql"], ["-seq", "-digits", "1", "ten"]),  # next number needs more digits
        (["999999_last.up.sql"], ["-seq", "overflow"]),
        (["notes.sql"], ["-seq", "next"]),  # malformed name
        (["bad_name.sql"], ["-seq", "next"]),  # non-numeric prefix
    ],
)
def test_sequential_refusals_leave_directory_unchanged(ws, existing, args):
    target = ws / "mig"
    for name in existing:
        write_fixture(target / name, "")
    fails(create(ws, "-ext", "sql", "-dir", "mig", *args))
    assert listing(target) == sorted(existing)


def test_sequential_collision_with_existing_version(ws):
    target = ws / "mig"
    existing = ["000003_a.down.sql", "000003_a.up.sql", "2_y.up.sql"]
    for name in existing:
        write_fixture(target / name, "")
    # The last listed name is 2_y.up.sql, so the next number is 000003, which already exists.
    fails(create(ws, "-ext", "sql", "-dir", "mig", "-seq", "z"))
    assert listing(target) == existing


@pytest.mark.parametrize(
    "args",
    [
        ["-ext", "sql", "-dir", "mig", "-seq", "-digits", "0", "e"],
        ["-ext", "sql", "-dir", "mig", "-seq", "-format", "2006", "e"],
        ["-ext", "sql", "-dir", "mig", "-format", "", "e"],
        ["-dir", "mig", "-seq", "e"],  # no -ext
        ["-ext", "sql", "-dir", "mig", "-seq"],  # no name
        ["-ext", "sql", "-dir", "mig", "-tz", "Mars/Base", "e"],
        ["-ext", "sql", "-dir", "mig", "-bogus", "e"],
    ],
)
def test_invalid_create_requests_write_nothing(ws, args):
    fails(create(ws, *args))
    target = ws / "mig"
    if os.path.lexists(target):
        assert stat.S_ISDIR(os.lstat(target).st_mode), "mig is not a real directory"
        assert os.listdir(target) == []
    assert all(name == "mig" for name in os.listdir(ws))


def stamp_close_to(text, now, layout="%Y%m%d%H%M%S"):
    stamp = datetime.strptime(text, layout).replace(tzinfo=now.tzinfo)
    assert abs(stamp - now) < timedelta(minutes=3), f"{text} is not the current time {now:{layout}}"


def test_default_version_is_utc_timestamp(ws):
    ok(create(ws, "-ext", "sql", "-dir", "ts", "stamp"))
    names = listing(ws / "ts")
    assert len(names) == 2
    version = names[0].split("_", 1)[0]
    assert names == [f"{version}_stamp.down.sql", f"{version}_stamp.up.sql"]
    assert len(version) == 14 and version.isdigit()
    stamp_close_to(version, datetime.now(timezone.utc))


def test_timestamp_in_requested_zone(ws):
    ok(create(ws, "-ext", "sql", "-dir", "tz", "-tz", "Asia/Tokyo", "stamp"))
    names = listing(ws / "tz")
    version = names[0].split("_", 1)[0]
    assert names == [f"{version}_stamp.down.sql", f"{version}_stamp.up.sql"]
    stamp_close_to(version, datetime.now(ZoneInfo("Asia/Tokyo")))


def test_layout_and_unix_formats(ws):
    result = ok(create(ws, "-ext", "sql", "-dir", "f", "-format", "9", "fixed"))
    assert listing(ws / "f") == ["9_fixed.down.sql", "9_fixed.up.sql"]
    assert printed(result) == sorted([str(ws / "f" / "9_fixed.up.sql"), str(ws / "f" / "9_fixed.down.sql")])
    # A layout that yields the same version again is a duplicate.
    fails(create(ws, "-ext", "sql", "-dir", "f", "-format", "9", "again"))
    assert listing(ws / "f") == ["9_fixed.down.sql", "9_fixed.up.sql"]

    ok(create(ws, "-ext", "sql", "-dir", "u", "-format", "unix", "epoch"))
    names = listing(ws / "u")
    seconds = names[0].split("_", 1)[0]
    assert names == [f"{seconds}_epoch.down.sql", f"{seconds}_epoch.up.sql"]
    assert seconds.isdigit()
    assert abs(int(seconds) - datetime.now(timezone.utc).timestamp()) < 180
