"""Multi-run workflows: failure and --resume, completed work directories, --restart, snapshots."""

import time

import psycopg
import pytest

from dbtools import (
    check,
    cloner,
    constraints,
    digest,
    execute,
    indexes,
    relations,
    sequences,
    table_digests,
)
from harness import start_command
from sources import FAIL_SMALL_ON_TARGET, REMOVE_FAILURE, STEPS

LATE_ROWS = """
insert into public.big values (20001, 'late');
insert into public.mid values (3001, 1, 'late');
insert into public.small(label) values ('late');
insert into public.heap values ('late', 'late');
"""


@pytest.mark.timeout(600)
def test_failed_table_is_resumed_without_redoing_finished_work(source_db, target_db, work):
    execute(source_db, STEPS)
    execute(target_db, FAIL_SMALL_ON_TARGET)
    directory = work / "w"
    common = ["--source", source_db, "--target", target_db, "--dir", directory]

    first = cloner(["clone", *common, "--table-jobs", "2", "--index-jobs", "2"], work)
    assert first.returncode != 0
    finished = {table: digest(source_db, "public", table) for table in ("big", "mid", "heap")}
    for table, expected in finished.items():
        assert digest(target_db, "public", table) == expected, table
    assert digest(target_db, "public", "small")[0] == 0

    execute(target_db, REMOVE_FAILURE)
    execute(source_db, LATE_ROWS)

    # The snapshot of the failed run is gone: resuming it consistently is refused and changes nothing.
    assert cloner(["clone", *common, "--resume"], work).returncode != 0
    for table, expected in finished.items():
        assert digest(target_db, "public", table) == expected, table
    assert digest(target_db, "public", "small")[0] == 0

    check(cloner(["clone", *common, "--resume", "--not-consistent"], work))
    # Finished tables are not copied again (no late rows, no duplicates) ...
    for table, expected in finished.items():
        assert digest(target_db, "public", table) == expected, table
    # ... the failed table is copied from a new snapshot, and the remaining work completes.
    assert digest(target_db, "public", "small") == digest(source_db, "public", "small")
    assert digest(target_db, "public", "small")[0] == 6
    assert indexes(target_db) == indexes(source_db)
    assert constraints(target_db) == constraints(source_db)
    assert sequences(target_db)[("public", "gseq")] == (31337, True)

    # Resuming a completed run does nothing more.
    execute(source_db, "insert into public.small(label) values ('later')")
    state = table_digests(target_db)
    check(cloner(["clone", *common, "--resume", "--not-consistent"], work))
    assert table_digests(target_db) == state


def test_completed_work_directory_is_not_redone(source_db, target_db, work):
    execute(source_db, STEPS)
    common = ["--source", source_db, "--target", target_db, "--dir", work / "w"]
    check(cloner(["clone", *common], work))
    state = table_digests(target_db)
    assert state == table_digests(source_db)
    execute(source_db, LATE_ROWS + "select setval('public.gseq', 50000);")
    check(cloner(["clone", *common], work))
    assert table_digests(target_db) == state
    assert sequences(target_db)[("public", "gseq")] == (31337, True)
    assert indexes(target_db) == indexes(source_db)


def test_work_directory_belongs_to_its_target_until_restart(source_db, target_db, make_db, target_instance, work):
    execute(source_db, STEPS)
    directory = work / "w"
    check(cloner(["clone", "--source", source_db, "--target", target_db, "--dir", directory], work))
    execute(source_db, LATE_ROWS)
    other = make_db(target_instance)

    result = cloner(["clone", "--source", source_db, "--target", other, "--dir", directory], work)
    assert result.returncode != 0
    assert relations(other) == set()

    result = cloner(["clone", "--source", source_db, "--target", other, "--dir", directory, "--resume", "--restart"], work)
    assert result.returncode != 0
    assert relations(other) == set()

    check(cloner(["clone", "--source", source_db, "--target", other, "--dir", directory, "--restart"], work))
    assert table_digests(other) == table_digests(source_db)
    assert digest(other, "public", "small")[0] == 6
    assert indexes(other) == indexes(source_db)
    assert constraints(other) == constraints(source_db)
    # The original target is left as it was.
    assert digest(target_db, "public", "small")[0] == 5


SNAPSHOT = """
create table public.t1 (id int primary key, pad text);
insert into public.t1 select g, repeat(md5(g::text), 4) from generate_series(1, 60000) g;
create table public.t2 (id int primary key, pad text);
insert into public.t2 select g, repeat(md5(g::text), 4) from generate_series(1, 20000) g;
create table public.t3 (id int primary key, pad text);
insert into public.t3 select g, repeat(md5(g::text), 4) from generate_series(1, 10000) g;
create table public.t4 (id int primary key, pad text);
insert into public.t4 select g, 'small' from generate_series(1, 10) g;
"""


def test_all_tables_come_from_one_snapshot(source_db, target_db, work):
    """Rows committed on the source while the copy runs are not copied, even into tables copied later."""
    execute(source_db, SNAPSHOT)
    expected = table_digests(source_db)
    process = start_command(
        ["/app/program", "clone", "--source", source_db, "--target", target_db, "--dir", str(work / "w"), "--table-jobs", "1", "--index-jobs", "1"],
        cwd=work,
        env={"TMPDIR": str(work)},
    )
    blocker = None
    try:
        with psycopg.connect(target_db, autocommit=True) as poll:
            deadline = time.monotonic() + 30
            while not poll.execute("select to_regclass('public.t4') is not null").fetchone()[0]:
                assert process.poll() is None, "program exited before creating the target tables"
                assert time.monotonic() < deadline, "target table t4 never appeared"
                time.sleep(0.01)
        # Hold the last (smallest) table so the copy cannot finish before the source changes.
        blocker = psycopg.connect(target_db)
        blocker.execute("lock table public.t4 in access exclusive mode")
        execute(source_db, "insert into public.t1 values (-1, 'new'); insert into public.t2 values (-1, 'new');"
                           "insert into public.t3 values (-1, 'new'); insert into public.t4 values (-1, 'new');")
        time.sleep(2)
        blocker.rollback()
        blocker.close()
        blocker = None
        assert process.wait(timeout=40) == 0
    finally:
        if blocker is not None:
            blocker.close()
        if process.poll() is None:
            process.kill()
        process.wait()
    assert table_digests(target_db) == expected
