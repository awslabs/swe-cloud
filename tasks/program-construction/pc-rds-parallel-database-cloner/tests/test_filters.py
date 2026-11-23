"""Filter files: selection in list commands and its consequences for a filtered clone."""

import pytest

from dbtools import (
    check,
    cloner,
    constraint_names,
    digest,
    execute,
    index_names,
    parse_table,
    query,
    relations,
    schemas,
    sequences,
    write_filters,
)
from sources import FILTERING

EXCLUDING = """# exclusions of every kind
[exclude-schema]
arch

[exclude-table]
app.events

[exclude-table-data]
misc.kv

[exclude-index]
app.orders_total_idx
"""

INCLUDING = """[include-only-table]
app.orders
; a comment line
app.notes
"""


def listing(source, work, filters, command, *extra, directory):
    result = check(cloner(["list", command, "--source", source, "--filters", filters, "--dir", directory, *extra], work))
    return parse_table(result.stdout)


@pytest.mark.timeout(600)
def test_list_with_exclusions(source_db, work):
    execute(source_db, FILTERING)
    filters = write_filters(work, "f.ini", EXCLUDING)
    directory = work / "w"
    tables = listing(source_db, work, filters, "tables", directory=directory)
    assert tables.values("Schema Name", "Table Name") == [("app", "notes"), ("app", "orders"), ("app", "users"), ("misc", "kv")]
    skipped = listing(source_db, work, filters, "tables", "--list-skipped", directory=directory)
    assert skipped.values("Schema Name", "Table Name") == [("app", "events"), ("arch", "old_orders")]

    # In a work directory where `list tables` ran with the same filters:
    seqs = listing(source_db, work, filters, "sequences", directory=directory)
    assert seqs.values("Schema Name", "Sequence Name") == [("app", "notes_seq"), ("app", "orders_id_seq"), ("app", "users_id_seq"), ("public", "counter_seq")]
    skipped = listing(source_db, work, filters, "sequences", "--list-skipped", directory=directory)
    assert skipped.values("Schema Name", "Sequence Name") in (
        [("app", "events_id_seq")],
        [("app", "events_id_seq"), ("arch", "arch_seq")],
    )
    idx = listing(source_db, work, filters, "indexes", directory=directory)
    assert idx.values("Schema", "Index Name") == [
        ("app", "notes_pkey"),
        ("app", "orders_pkey"),
        ("app", "orders_user_idx"),
        ("app", "users_name_key"),
        ("app", "users_pkey"),
        ("misc", "kv_pkey"),
    ]
    depends = listing(source_db, work, filters, "depends", "--list-skipped", directory=directory)
    assert depends.values("Schema Name", "Table Name", "Type", "Identity") == [
        ("app", "events", "sequence", "app.events_id_seq"),
        ("app", "events", "table constraint", "events_pkey on app.events"),
        ("app", "events", "view", "misc.v_events"),
        ("arch", "old_orders", "table constraint", "old_orders_pkey on arch.old_orders"),
        ("arch", "old_orders", "table constraint", "old_orders_user_id_fkey on arch.old_orders"),
    ]


def test_list_with_include_only(source_db, work):
    execute(source_db, FILTERING)
    filters = write_filters(work, "f.ini", INCLUDING)
    directory = work / "w"
    tables = listing(source_db, work, filters, "tables", directory=directory)
    assert tables.values("Schema Name", "Table Name") == [("app", "notes"), ("app", "orders")]
    oids = dict(query(source_db, "select relname, oid::text from pg_class where relname in ('notes', 'orders')"))
    assert sorted(tables.values("Table Name", "OID")) == sorted(oids.items())
    skipped = listing(source_db, work, filters, "tables", "--list-skipped", directory=directory)
    assert skipped.values("Schema Name", "Table Name") == [("app", "events"), ("app", "users"), ("arch", "old_orders"), ("misc", "kv")]

    seqs = listing(source_db, work, filters, "sequences", directory=directory)
    assert seqs.values("Schema Name", "Sequence Name") == [("app", "notes_seq"), ("app", "orders_id_seq"), ("arch", "arch_seq"), ("public", "counter_seq")]
    skipped = listing(source_db, work, filters, "sequences", "--list-skipped", directory=directory)
    assert skipped.values("Schema Name", "Sequence Name") == [("app", "events_id_seq"), ("app", "users_id_seq")]
    idx = listing(source_db, work, filters, "indexes", directory=directory)
    assert idx.values("Schema", "Index Name") == [("app", "notes_pkey"), ("app", "orders_pkey"), ("app", "orders_total_idx"), ("app", "orders_user_idx")]


def test_clone_with_exclusions(source_db, target_db, work):
    execute(source_db, FILTERING)
    filters = write_filters(work, "f.ini", EXCLUDING)
    check(cloner(["clone", "--source", source_db, "--target", target_db, "--filters", filters, "--dir", work / "w"], work))

    assert relations(target_db) == {
        ("app", "users", "r"), ("app", "orders", "r"), ("app", "notes", "r"), ("misc", "kv", "r"),
        ("app", "big_orders", "v"), ("app", "user_names", "v"), ("misc", "mv_kv", "m"),
        ("app", "users_id_seq", "S"), ("app", "orders_id_seq", "S"), ("app", "notes_seq", "S"), ("public", "counter_seq", "S"),
    }  # fmt: skip
    assert schemas(target_db) == {"app", "misc"}
    assert index_names(target_db) == {
        ("app", "users_pkey"), ("app", "users_name_key"), ("app", "orders_pkey"),
        ("app", "orders_user_idx"), ("app", "notes_pkey"), ("misc", "kv_pkey"),
    }  # fmt: skip
    assert constraint_names(target_db) == {
        ("app", "users", "users_pkey"), ("app", "users", "users_name_key"), ("app", "orders", "orders_pkey"),
        ("app", "orders", "orders_total_check"), ("app", "orders", "orders_user_id_fkey"),
        ("app", "notes", "notes_pkey"), ("misc", "kv", "kv_pkey"),
    }  # fmt: skip
    for table in ("users", "orders", "notes"):
        assert digest(target_db, "app", table) == digest(source_db, "app", table)
    assert digest(target_db, "misc", "kv")[0] == 0
    assert query(target_db, "select count(*) from misc.mv_kv") == [(0,)]
    source_sequences = sequences(source_db)
    assert sequences(target_db) == {name: source_sequences[name] for name in sequences(target_db)}


def test_clone_with_include_only(source_db, target_db, work):
    execute(source_db, FILTERING)
    filters = write_filters(work, "f.ini", INCLUDING)
    check(cloner(["clone", "--source", source_db, "--target", target_db, "--filters", filters, "--dir", work / "w"], work))

    # Only the listed tables, the view that depends on them alone, and the sequences they use or
    # that no table owns; the foreign key to the excluded users table is dropped.
    assert relations(target_db) == {
        ("app", "orders", "r"), ("app", "notes", "r"), ("app", "big_orders", "v"),
        ("app", "orders_id_seq", "S"), ("app", "notes_seq", "S"), ("arch", "arch_seq", "S"), ("public", "counter_seq", "S"),
    }  # fmt: skip
    assert index_names(target_db) == {("app", "orders_pkey"), ("app", "orders_user_idx"), ("app", "orders_total_idx"), ("app", "notes_pkey")}
    assert constraint_names(target_db) == {("app", "orders", "orders_pkey"), ("app", "orders", "orders_total_check"), ("app", "notes", "notes_pkey")}
    for table in ("orders", "notes"):
        assert digest(target_db, "app", table) == digest(source_db, "app", table)
    source_sequences = sequences(source_db)
    assert sequences(target_db) == {
        name: source_sequences[name] for name in [("app", "orders_id_seq"), ("app", "notes_seq"), ("arch", "arch_seq"), ("public", "counter_seq")]
    }
    assert sequences(target_db)[("arch", "arch_seq")] == (77, True)
    assert query(target_db, "select count(*) from app.big_orders") == [(50,)]


def test_exclude_table_data_and_index_on_one_table(source_db, target_db, work):
    execute(source_db, FILTERING)
    filters = write_filters(work, "f.ini", "[include-only-table]\napp.users\napp.orders\n\n[exclude-table-data]\napp.orders\n\n[exclude-index]\napp.orders_user_idx\n")
    check(cloner(["clone", "--source", source_db, "--target", target_db, "--filters", filters, "--dir", work / "w"], work))
    assert {name for schema, name, kind in relations(target_db) if kind == "r"} == {"users", "orders"}
    assert digest(target_db, "app", "users") == digest(source_db, "app", "users")
    assert digest(target_db, "app", "orders")[0] == 0
    assert index_names(target_db) == {("app", "users_pkey"), ("app", "users_name_key"), ("app", "orders_pkey"), ("app", "orders_total_idx")}
    assert ("app", "orders", "orders_user_id_fkey") in constraint_names(target_db)
    # Sequences of included tables still carry the source values.
    assert sequences(target_db)[("app", "orders_id_seq")] == (100, True)
