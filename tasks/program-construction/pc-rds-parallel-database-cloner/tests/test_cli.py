"""Invocation: connection settings, option/environment precedence, listing output, usage errors."""

import pytest

from dbtools import check, cloner, execute, exists, parse_table, query, relations, write_filters
from sources import FILTERING

SMALL = """
create schema inv;
create table inv.items (id int primary key, label text);
create table inv.stock (item_id int references inv.items(id), qty int);
create table public.loose (v text);
insert into inv.items select g, 'i' || g from generate_series(1, 30) g;
insert into inv.stock select g, g * 2 from generate_series(1, 30) g;
"""


def source_tables(uri):
    rows = query(
        uri,
        """select n.nspname, c.relname, c.oid::text from pg_class c join pg_namespace n on n.oid = c.relnamespace
            where c.relkind = 'r' and n.nspname !~ '^pg_' and n.nspname <> 'information_schema'""",
    )
    return sorted(rows)


@pytest.mark.timeout(600)
def test_list_tables_shows_every_source_table(source_db, work):
    execute(source_db, SMALL)
    result = check(cloner(["list", "tables", "--source", source_db, "--dir", work / "w"], work))
    table = parse_table(result.stdout)
    assert table.values("Schema Name", "Table Name", "OID") == [(s, n, o) for s, n, o in source_tables(source_db)]


def test_environment_supplies_connections_and_jobs(make_db, source_instance, target_instance, work):
    source = make_db(source_instance)
    target = make_db(target_instance)
    execute(source, SMALL)
    env = {
        "PGCOPYDB_SOURCE_PGURI": source,
        "PGCOPYDB_TARGET_PGURI": target,
        "PGCOPYDB_TABLE_JOBS": "2",
        "PGCOPYDB_INDEX_JOBS": "2",
    }
    check(cloner(["clone", "--dir", work / "w"], work, env=env))
    assert relations(target) == relations(source)
    assert query(target, "select count(*), sum(qty) from inv.stock") == [(30, 930)]


def test_options_take_precedence_over_environment(make_db, source_instance, work):
    chosen = make_db(source_instance)
    other = make_db(source_instance)
    execute(chosen, SMALL)
    execute(other, "create table public.decoy (x int);")
    env = {"PGCOPYDB_SOURCE_PGURI": other}
    result = check(cloner(["list", "tables", "--source", chosen, "--dir", work / "w"], work, env=env))
    assert parse_table(result.stdout).values("Schema Name", "Table Name") == [("inv", "items"), ("inv", "stock"), ("public", "loose")]


def test_keyword_value_connection_strings(make_db, source_instance, target_instance, work):
    source_name = make_db(source_instance).rsplit("/", 1)[1]
    target_name = make_db(target_instance).rsplit("/", 1)[1]
    source, target = source_instance.uri(source_name), target_instance.uri(target_name)
    execute(source, SMALL)
    check(
        cloner(
            ["clone", "--source", source_instance.keyvalue(source_name), "--target", target_instance.keyvalue(target_name), "--dir", work / "w"],
            work,
        )
    )
    assert query(target, "select count(*) from inv.items") == [(30,)]
    assert relations(target) == relations(source)


def test_missing_connections_are_usage_errors(source_db, target_db, work):
    execute(source_db, SMALL)
    result = cloner(["list", "tables", "--dir", work / "a"], work)
    assert result.returncode != 0
    result = cloner(["clone", "--source", source_db, "--dir", work / "b"], work)
    assert result.returncode != 0
    result = cloner(["clone", "--target", target_db, "--dir", work / "c"], work)
    assert result.returncode != 0
    assert relations(target_db) == set()


def test_list_skipped_requires_filters(source_db, work):
    execute(source_db, SMALL)
    result = cloner(["list", "tables", "--source", source_db, "--list-skipped", "--dir", work / "w"], work)
    assert result.returncode != 0


def test_conflicting_filter_sections_are_rejected(source_db, target_db, work):
    execute(source_db, FILTERING)
    filters = write_filters(work, "conflict.ini", "[include-only-table]\napp.orders\n\n[exclude-table]\napp.users\n")
    result = cloner(["list", "tables", "--source", source_db, "--filters", filters, "--dir", work / "l"], work)
    assert result.returncode != 0
    result = cloner(["clone", "--source", source_db, "--target", target_db, "--filters", filters, "--dir", work / "c"], work)
    assert result.returncode != 0
    assert relations(target_db) == set()


def test_unknown_filter_names(source_db, target_db, work):
    """Included tables must exist (names are case-sensitive); exclusions of absent objects are harmless."""
    execute(source_db, FILTERING)
    for text in ("[include-only-table]\nApp.Orders\n", "[include-only-table]\napp.orders\napp.no_such_table\n"):
        filters = write_filters(work, "include.ini", text)
        result = cloner(["list", "tables", "--source", source_db, "--filters", filters, "--dir", work / "l1"], work)
        assert result.returncode != 0, text
        result = cloner(["clone", "--source", source_db, "--target", target_db, "--filters", filters, "--dir", work / "c1"], work)
        assert result.returncode != 0, text
        assert relations(target_db) == set()

    filters = write_filters(
        work, "exclude.ini", "[exclude-table]\napp.no_such_table\n[exclude-schema]\nno_such_schema\n[exclude-index]\napp.no_such_idx\n"
    )
    result = check(cloner(["list", "tables", "--source", source_db, "--filters", filters, "--dir", work / "l2"], work))
    assert parse_table(result.stdout).values("Schema Name", "Table Name") == [
        ("app", "events"), ("app", "notes"), ("app", "orders"), ("app", "users"), ("arch", "old_orders"), ("misc", "kv"),
    ]  # fmt: skip


def test_missing_filter_file(source_db, target_db, work):
    execute(source_db, SMALL)
    result = cloner(["clone", "--source", source_db, "--target", target_db, "--filters", work / "absent.ini", "--dir", work / "w"], work)
    assert result.returncode != 0
    assert not exists(target_db, "inv.items")
