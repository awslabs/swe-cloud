"""Full clone fidelity: every object kind and value survives, independent of worker counts."""

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
    parse_table,
    query,
    relations,
    schemas,
    sequences,
    table_digests,
    write_filters,
)
from harness import start_command
from sources import RICH, STEPS


def assert_same_database(source, target):
    assert schemas(target) == schemas(source)
    assert relations(target) == relations(source)
    assert indexes(target) == indexes(source)
    assert constraints(target) == constraints(source)
    assert sequences(target) == sequences(source)
    assert table_digests(target) == table_digests(source)


@pytest.mark.timeout(600)
@pytest.mark.parametrize("jobs", [("1", "1"), ("4", "3")], ids=["serial", "parallel"])
def test_clone_reproduces_the_database(source_db, target_db, work, jobs):
    execute(source_db, RICH)
    table_jobs, index_jobs = jobs
    result = check(
        cloner(["clone", "--source", source_db, "--target", target_db, "--dir", work / "w", "--table-jobs", table_jobs, "--index-jobs", index_jobs], work)
    )
    assert_same_database(source_db, target_db)

    # Spot checks that do not rely on the inventory helpers.
    assert sequences(target_db)[("public", "called_false_seq")] == (50, False)
    assert sequences(target_db)[("public", "unused_seq")] == (671, False)
    assert sequences(target_db)[("public", "ticket_seq")] == (4242, True)
    assert sequences(target_db)[("shop", "orders_id_seq")] == (3000, True)
    assert query(target_db, "select count(*) from only public.parent_model") == [(1,)]
    assert query(target_db, "select id, nom, up from public.gen order by id") == [(1, "abc", "ABC"), (2, None, None), (3, "Été", "ÉTÉ")]
    assert query(target_db, "select count(*) from public.nocols") == [(2,)]
    assert query(target_db, "select count(*) from public.sellers where id = 3") == [(2,)]
    assert query(target_db, "select convalidated from pg_constraint where conname = 'chk_name'") == [(False,)]
    assert query(target_db, "select name from public.employees order by 1") == [("abc",), ("zed",)]
    # The materialized view is populated with the same content as on the source.
    assert query(target_db, "select ispopulated from pg_matviews where matviewname = 'mv'") == [(True,)]
    assert digest(target_db, "public", "mv", only=False) == digest(source_db, "public", "mv", only=False)
    assert query(target_db, "select note from shop.orders where id = 5") == [('tab\there\nnew "line" \\ back',)]
    assert query(target_db, r"select convert_from(lo_get(424242), 'UTF8')") == [("hello large object",)]

    # The per-table summary on stdout names every copied table.
    summary = parse_table(result.stdout)
    assert summary.values("Schema", "Name") == sorted((s, t) for s, t in table_digests(source_db))


def test_skip_large_objects(source_db, target_db, work):
    execute(source_db, RICH)
    check(cloner(["clone", "--source", source_db, "--target", target_db, "--dir", work / "w", "--skip-large-objects"], work))
    assert table_digests(target_db) == table_digests(source_db)
    # Large object contents are not transferred.
    assert query(target_db, "select count(*) from pg_largeobject where loid = 424242") == [(0,)]


def test_split_large_tables(source_db, target_db, make_db, target_instance, work):
    execute(source_db, STEPS)
    execute(
        source_db,
        """create table public.wide (k text, pad text);
           insert into public.wide select md5((g % 900)::text), repeat('y', 300) from generate_series(1, 9000) g;
           create table public.keyed (id bigint primary key, pad text);
           insert into public.keyed select g, repeat(md5(g::text), 4) from generate_series(1, 20000) g;""",
    )
    result = check(
        cloner(["clone", "--source", source_db, "--target", target_db, "--dir", work / "split", "--split-tables-larger-than", "1MB", "--table-jobs", "4"], work)
    )
    # Every row arrives exactly once, including duplicate rows of a table without a key.
    assert table_digests(target_db) == table_digests(source_db)
    summary = parse_table(result.stdout)
    parts = {(schema, name): int(count) for schema, name, count in summary.values("Schema", "Name", "Parts")}
    assert parts[("public", "keyed")] > 1
    assert parts[("public", "wide")] > 1
    assert parts[("public", "small")] == 1
    assert parts[("public", "mid")] == 1

    # Without the option, no table is split.
    unsplit = make_db(target_instance)
    result = check(cloner(["clone", "--source", source_db, "--target", unsplit, "--dir", work / "whole", "--table-jobs", "4"], work))
    assert {int(count) for (count,) in parse_table(result.stdout).values("Parts")} == {1}
    assert table_digests(unsplit) == table_digests(source_db)


def test_split_table_parts_are_copied_concurrently(source_db, make_db, target_instance, work):
    execute(source_db, """create table public.keyed (id bigint primary key, pad text);
        insert into public.keyed select g, repeat(md5(g::text), 4) from generate_series(1, 20000) g;""")
    # Hold row insertion, rather than table setup or TRUNCATE, so that two
    # actual writers of ONE split table can reach the same deterministic gate.
    observed = make_db(target_instance)
    execute(observed, """
        create table public.keyed (id bigint, pad text);
        create function public.hold_insert() returns trigger language plpgsql as $$
        begin
            perform pg_advisory_xact_lock_shared(734291);
            return new;
        end $$;
        create trigger hold_insert before insert on public.keyed
            for each row execute function public.hold_insert();
        alter table public.keyed enable always trigger hold_insert;
    """)
    filters = write_filters(work, "observe.ini", "[include-only-table]\npublic.keyed\n")
    process = None
    with psycopg.connect(observed, autocommit=True) as blocker:
        blocker.execute("select pg_advisory_lock(734291)")
        try:
            process = start_command(
                ["/app/program", "copy", "table-data", "--source", source_db,
                 "--target", observed, "--dir", str(work / "observe"),
                 "--filters", str(filters), "--split-tables-larger-than", "1MB",
                 "--table-jobs", "4"],
                cwd=work, env={"TMPDIR": str(work)},
            )
            deadline = time.monotonic() + 40
            while True:
                # No SQL spelling or transfer protocol is assumed: COPY and
                # INSERT both invoke the trigger. Only client backends count.
                waiting = blocker.execute("""
                    select count(distinct l.pid)
                      from pg_locks l join pg_stat_activity a on a.pid = l.pid
                     where l.locktype = 'advisory' and not l.granted
                       and l.database = (select oid from pg_database where datname = current_database())
                       and l.classid = 0 and l.objid = 734291 and l.objsubid = 1
                       and a.backend_type = 'client backend'
                """).fetchone()[0]
                if waiting >= 2:
                    break
                assert process.poll() is None, "program exited before two split writers reached the insert gate"
                assert time.monotonic() < deadline, f"expected overlapping split writers; observed {waiting}"
                time.sleep(0.05)
            blocker.execute("select pg_advisory_unlock(734291)")
            assert process.wait(timeout=30) == 0
        finally:
            blocker.execute("select pg_advisory_unlock_all()")
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait()
    assert digest(observed, "public", "keyed") == digest(source_db, "public", "keyed")
