"""Individual copy steps and the compare commands."""

import json
import subprocess

import pytest

from dbtools import (
    check,
    cloner,
    constraint_names,
    constraints,
    digest,
    execute,
    index_names,
    indexes,
    parse_table,
    query,
    relations,
    sequences,
    table_digests,
)
from sources import FILTERING, STEPS

RESUME = ["--resume", "--not-consistent"]


def pre_data(source, target):
    """Create the target tables (no indexes, constraints or rows) with the standard client tools."""
    dump = subprocess.run(["pg_dump", "--section=pre-data", "--no-owner", source], capture_output=True, text=True, check=True, timeout=60)
    subprocess.run(["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", target], input=dump.stdout, capture_output=True, text=True, check=True, timeout=60)


def compare_report(stdout):
    """[(table name, marker, source checksum, target checksum)] from the `compare data` text table."""
    table = parse_table(stdout)
    return table.values("Table Name", "!", "Source Checksum", "Target Checksum")


def primary_and_unique(uri):
    return {(s, t, n) for s, t, n, kind, _ in constraints(uri) if kind in ("p", "u")}


@pytest.mark.timeout(600)
def test_copy_schema_creates_definitions_without_rows(source_db, target_db, work):
    execute(source_db, STEPS)
    check(cloner(["copy", "schema", "--source", source_db, "--target", target_db, "--dir", work / "w"], work))
    assert relations(target_db) == relations(source_db)
    assert indexes(target_db) == indexes(source_db)
    assert constraints(target_db) == constraints(source_db)
    assert {table: count for table, (count, _) in table_digests(target_db).items()} == {
        ("public", "big"): 0, ("public", "mid"): 0, ("public", "small"): 0, ("public", "heap"): 0,
    }  # fmt: skip
    assert sequences(target_db)[("public", "gseq")] != (31337, True)


def test_step_by_step_copy(source_db, target_db, work):
    execute(source_db, STEPS)
    pre_data(source_db, target_db)
    before_sequences = sequences(target_db)
    directory = work / "w"
    common = ["--source", source_db, "--target", target_db, "--dir", directory]

    check(cloner(["copy", "table-data", *common], work))
    assert table_digests(target_db) == table_digests(source_db)
    assert index_names(target_db) == set()
    assert sequences(target_db) == before_sequences

    check(cloner(["copy", "sequences", *common, *RESUME], work))
    assert sequences(target_db) == sequences(source_db)
    assert index_names(target_db) == set()

    check(cloner(["copy", "indexes", *common, *RESUME], work))
    assert indexes(target_db) == indexes(source_db)
    assert primary_and_unique(target_db) == primary_and_unique(source_db)

    check(cloner(["copy", "constraints", *common, *RESUME], work))
    assert primary_and_unique(target_db) == primary_and_unique(source_db)
    assert indexes(target_db) == indexes(source_db)
    assert table_digests(target_db) == table_digests(source_db)


def test_copy_constraints_on_a_target_without_indexes(source_db, target_db, work):
    """The constraints step builds the primary key and unique constraints (with their indexes)."""
    execute(source_db, STEPS)
    pre_data(source_db, target_db)
    common = ["--source", source_db, "--target", target_db, "--dir", work / "w"]
    check(cloner(["copy", "table-data", *common], work))
    assert primary_and_unique(target_db) == set()
    # The exit status is not graded here: the reference creates everything, then reports an error.
    cloner(["copy", "constraints", *common, *RESUME], work)
    assert primary_and_unique(target_db) == primary_and_unique(source_db)
    assert table_digests(target_db) == table_digests(source_db)


def test_copy_data_into_existing_schema(source_db, target_db, work):
    execute(source_db, STEPS)
    pre_data(source_db, target_db)
    check(cloner(["copy", "data", "--source", source_db, "--target", target_db, "--dir", work / "w", "--table-jobs", "2"], work))
    assert relations(target_db) == relations(source_db)
    assert table_digests(target_db) == table_digests(source_db)
    assert indexes(target_db) == indexes(source_db)
    assert primary_and_unique(target_db) == primary_and_unique(source_db)
    assert sequences(target_db) == sequences(source_db)


def test_compare_schema(source_db, target_db, work):
    execute(source_db, FILTERING)
    directory = work / "w"
    common = ["--source", source_db, "--target", target_db, "--dir", directory]
    check(cloner(["clone", *common], work))
    check(cloner(["compare", "schema", *common], work))

    execute(target_db, "drop index app.orders_total_idx")
    assert cloner(["compare", "schema", *common], work).returncode != 0
    execute(target_db, "create index orders_total_idx on app.orders(total)")
    check(cloner(["compare", "schema", *common], work))

    execute(target_db, "select setval('public.counter_seq', 5)")
    assert cloner(["compare", "schema", *common], work).returncode != 0
    execute(target_db, "select setval('public.counter_seq', 9001)")
    check(cloner(["compare", "schema", *common], work))

    execute(target_db, "drop table app.notes")
    assert cloner(["compare", "schema", *common], work).returncode != 0


def test_compare_data(source_db, target_db, work):
    execute(source_db, FILTERING)
    database = source_db.rsplit("/", 1)[1]
    directory = work / "w"
    common = ["--source", source_db, "--target", target_db, "--dir", directory]
    check(cloner(["clone", *common], work))
    tables = {("app", "users"), ("app", "orders"), ("app", "events"), ("app", "notes"), ("arch", "old_orders"), ("misc", "kv")}

    result = check(cloner(["compare", "data", *common], work))
    report = compare_report(result.stdout)
    assert sorted(name for name, _, _, _ in report) == sorted(f"{database}.{schema}.{table}" for schema, table in tables)
    for _, marker, source_sum, target_sum in report:
        assert marker == ""
        assert source_sum == target_sum != ""

    # Same multiset of rows in a different physical order still matches; changed data does not.
    execute(target_db, "update app.orders set total = total where id % 2 = 0")
    execute(
        target_db,
        """update app.users set name = 'renamed' where id = 3;
           delete from misc.kv where k = 'k2';
           insert into app.notes values (99, 'extra');""",
    )
    result = check(cloner(["compare", "data", *common], work))
    rows = compare_report(result.stdout)
    report = {name: (marker, source_sum, target_sum) for name, marker, source_sum, target_sum in rows}
    assert len(rows) == len(report)
    assert set(report) == {f"{database}.{schema}.{table}" for schema, table in tables}
    changed = {f"{database}.app.users", f"{database}.misc.kv", f"{database}.app.notes"}
    for name, (marker, source_sum, target_sum) in report.items():
        if name in changed:
            assert marker == "!", name
            assert source_sum != target_sum, name
        else:
            assert marker == "", name
            assert source_sum == target_sum, name

    result = check(cloner(["compare", "data", *common, "--json"], work))
    entries = json.loads(result.stdout)
    assert isinstance(entries, list)
    by_name = {(entry["schema"], entry["name"]): entry for entry in entries}
    assert len(entries) == len(by_name) == len(tables)
    assert set(by_name) == tables
    expected_counts = {
        ("app", "users"): (10, 10), ("app", "orders"): (100, 100), ("app", "events"): (5, 5),
        ("app", "notes"): (4, 5), ("arch", "old_orders"): (3, 3), ("misc", "kv"): (6, 5),
    }  # fmt: skip
    for key, (source_rows, target_rows) in expected_counts.items():
        entry = by_name[key]
        for side, rows in (("source", source_rows), ("target", target_rows)):
            assert type(entry[side]["rowcount"]) in (int, float) and entry[side]["rowcount"] == rows, (key, side)
            assert isinstance(entry[side]["checksum"], str)
        same = entry["source"]["checksum"] == entry["target"]["checksum"]
        assert same == (key not in {("app", "users"), ("misc", "kv"), ("app", "notes")}), key
        # The JSON checksums agree with the text report.
        text = report[f"{database}.{key[0]}.{key[1]}"]
        assert (entry["source"]["checksum"], entry["target"]["checksum"]) == text[1:]

    # A table missing on the target makes the comparison fail.
    execute(target_db, "drop table app.notes")
    assert cloner(["compare", "data", *common], work).returncode != 0
    # The source is never modified by any of this.
    assert digest(source_db, "app", "notes")[0] == 4
    assert query(source_db, "select name from app.users where id = 3") == [("n3",)]
