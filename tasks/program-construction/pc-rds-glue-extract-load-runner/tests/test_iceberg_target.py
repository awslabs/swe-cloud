"""Single replications into Iceberg tables on Glue: catalog registration, schema typing and the
logical rows of the current snapshot under each mode. Expectations observed from the reference."""

import datetime as dt
import json
import time
import uuid
from decimal import Decimal

from synclab import create_orders, create_typed, dict_rows, failed, ok

UTC = dt.timezone.utc
ORDER_FIELDS = [("id", "int"), ("customer", "string"), ("amount", "decimal(24, 6)"), ("updated_at", "timestamptz")]


def ts(*args):
    return dt.datetime(*args, tzinfo=UTC)


def order(i, customer, amount, updated_at):
    return {"id": i, "customer": customer, "amount": amount, "updated_at": updated_at}


BASE = [order(1, "ann", Decimal("10.5"), ts(2024, 1, 1, 10)), order(2, "bob", Decimal("20"), ts(2024, 1, 2, 10)),
        order(3, "cy", None, ts(2024, 1, 3, 10))]


def stream(lab, table, mode, extra=""):
    return f"""
source: SRC
target: LAKE
streams:
  sales.orders:
    object: '{lab.glue_db}.{table}'
    mode: {mode}
{extra}"""


CHANGES = ("update sales.orders set customer = 'ann2', updated_at = '2024-02-01' where id = 1;"
           " insert into sales.orders values (4, 'dee', 4, '2024-02-02');"
           " update sales.orders set customer = 'bob2' where id = 2")


def _uuid(value):
    return uuid.UUID(bytes=value) if isinstance(value, bytes) and len(value) == 16 else value


def test_table_is_registered_in_glue_with_mapped_types(lab):
    create_typed(lab)
    ok(lab.replicate(f"""
source: SRC
target: LAKE
streams:
  sales.typed:
    object: '{lab.glue_db}.typed'
"""))
    assert lab.glue_tables() == ["typed"]
    params = lab.glue_table("typed")["Parameters"]
    assert params["table_type"].upper() == "ICEBERG"
    assert params["metadata_location"].startswith(f"s3://{lab.bucket}/warehouse/")
    table = lab.iceberg("typed")
    assert table.fields == [
        ("id", "int"), ("s", "int"), ("b", "long"), ("n", "decimal(24, 6)"), ("r", "double"), ("d", "double"),
        ("flag", "boolean"), ("t", "string"), ("v", "string"), ("ch", "string"), ("dt", "date"),
        ("ts", "timestamptz"), ("tz", "timestamptz"), ("j", "string"), ("js", "string"), ("u", "uuid"),
        ("tm", "time"), ("by", "binary")]
    rows = sorted(table.rows, key=lambda r: r["id"])
    for r in rows:
        r["u"] = _uuid(r["u"])
    expected = [
        {"id": 1, "s": 7, "b": 9007199254740993, "n": Decimal("10.5"), "r": 1.5, "d": 2.25, "flag": True,
         "t": 'he said "hi", ok', "v": "v1", "ch": "ab ", "dt": dt.date(2024, 3, 4),
         "ts": ts(2024, 1, 2, 10, 0, 0, 123456), "tz": ts(2024, 1, 2, 8), "j": '{"a": [1, 2], "b": null}',
         "js": '{"x":1}', "u": uuid.UUID("a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"), "tm": dt.time(13, 14, 15),
         "by": b"\x01\x02"},
        {"id": 2, "s": None, "b": None, "n": None, "r": None, "d": None, "flag": False, "t": "line1\nline2",
         "v": None, "ch": None, "dt": None, "ts": None, "tz": None, "j": None, "js": None, "u": None, "tm": None,
         "by": None},
    ]
    for r in rows + expected:
        for name in ("j", "js"):
            if r[name] is not None:
                r[name] = json.loads(r[name])
    assert rows == expected
    assert type(rows[0]["flag"]) is bool


def test_full_refresh_replaces_rows_and_schema(lab):
    create_orders(lab)
    spec = stream(lab, "fr", "full-refresh")
    ok(lab.replicate(spec))
    assert lab.iceberg("fr").fields == ORDER_FIELDS
    first = lab.metadata_location("fr")
    lab.src(CHANGES + "; alter table sales.orders drop column amount; alter table sales.orders add column region text;"
            " update sales.orders set region = 'eu' where id = 3")
    ok(lab.replicate(spec))
    assert lab.metadata_location("fr") != first
    table = lab.iceberg("fr")
    assert table.fields == [("id", "int"), ("customer", "string"), ("updated_at", "timestamptz"), ("region", "string")]
    assert dict_rows(table.rows) == dict_rows([
        {"id": 1, "customer": "ann2", "updated_at": ts(2024, 2, 1), "region": None},
        {"id": 2, "customer": "bob2", "updated_at": ts(2024, 1, 2, 10), "region": None},
        {"id": 3, "customer": "cy", "updated_at": ts(2024, 1, 3, 10), "region": "eu"},
        {"id": 4, "customer": "dee", "updated_at": ts(2024, 2, 2), "region": None},
    ])


def test_truncate_mode_is_rejected_without_touching_the_table(lab):
    create_orders(lab)
    ok(lab.replicate(stream(lab, "t1", "full-refresh")))
    before = lab.metadata_location("t1")
    failed(lab.replicate(stream(lab, "t1", "truncate")))
    assert lab.metadata_location("t1") == before
    assert dict_rows(lab.iceberg("t1").rows) == dict_rows(BASE)
    failed(lab.replicate(stream(lab, "t2", "truncate")))
    assert lab.glue_tables() == ["t1"]


def test_incremental_update_key_appends_newer_rows(lab):
    create_orders(lab)
    spec = stream(lab, "inc_u", "incremental", "    update_key: updated_at\n")
    ok(lab.replicate(spec))
    first = lab.metadata_location("inc_u")
    assert dict_rows(lab.iceberg("inc_u").rows) == dict_rows(BASE)
    lab.src(CHANGES)
    ok(lab.replicate(spec))
    second = lab.metadata_location("inc_u")
    assert second != first
    table = lab.iceberg("inc_u")
    assert table.fields == ORDER_FIELDS
    assert dict_rows(table.rows) == dict_rows(BASE + [
        order(1, "ann2", Decimal("10.5"), ts(2024, 2, 1)), order(4, "dee", Decimal("4"), ts(2024, 2, 2))])
    # Nothing past the maximum: the run succeeds and commits nothing.
    ok(lab.replicate(spec))
    assert lab.metadata_location("inc_u") == second


def test_incremental_primary_key_merges_in_place(lab):
    create_orders(lab)
    spec = stream(lab, "inc_pu", "incremental", "    primary_key: [id]\n    update_key: updated_at\n")
    ok(lab.replicate(spec))
    first = lab.metadata_location("inc_pu")
    assert dict_rows(lab.iceberg("inc_pu").rows) == dict_rows(BASE)
    lab.src(CHANGES)
    ok(lab.replicate(spec))
    second = lab.metadata_location("inc_pu")
    assert second != first
    table = lab.iceberg("inc_pu")
    assert table.fields == ORDER_FIELDS
    assert dict_rows(table.rows) == dict_rows([
        order(1, "ann2", Decimal("10.5"), ts(2024, 2, 1)), order(2, "bob", Decimal("20"), ts(2024, 1, 2, 10)),
        order(3, "cy", None, ts(2024, 1, 3, 10)), order(4, "dee", Decimal("4"), ts(2024, 2, 2))])
    ok(lab.replicate(spec))
    assert lab.metadata_location("inc_pu") == second


def test_primary_key_only_merge_with_composite_key_and_cast(lab):
    lab.src("create schema sales; create table sales.cp (a int, b text, v text, n bigint, primary key (a, b));"
            " insert into sales.cp values (1,'x','one',1),(1,'y','two',2),(2,'x','three',3)")
    spec = f"""
source: SRC
target: LAKE
streams:
  sales.cp:
    object: '{lab.glue_db}.cp'
    mode: incremental
    primary_key: [a, b]
    columns:
      a: bigint
"""
    ok(lab.replicate(spec))
    lab.src("update sales.cp set v = 'TWO' where a = 1 and b = 'y'; insert into sales.cp values (2,'y','four',4);"
            " delete from sales.cp where a = 2 and b = 'x'")
    ok(lab.replicate(spec))
    table = lab.iceberg("cp")
    assert table.fields == [("a", "long"), ("b", "string"), ("v", "string"), ("n", "long")]
    assert dict_rows(table.rows) == dict_rows([
        {"a": 1, "b": "x", "v": "one", "n": 1}, {"a": 1, "b": "y", "v": "TWO", "n": 2},
        {"a": 2, "b": "x", "v": "three", "n": 3}, {"a": 2, "b": "y", "v": "four", "n": 4}])


def test_snapshot_appends_stamped_copies(lab):
    create_orders(lab)
    spec = stream(lab, "snap", "snapshot")
    start = int(time.time())
    ok(lab.replicate(spec))
    end = int(time.time())
    table = lab.iceberg("snap")
    assert table.fields == ORDER_FIELDS + [("_sling_loaded_at", "int")]
    stamps = {r.pop("_sling_loaded_at") for r in table.rows}
    assert len(stamps) == 1
    stamp1 = stamps.pop()
    assert type(stamp1) is int and start - 1 <= stamp1 <= end + 1
    assert dict_rows(table.rows) == dict_rows(BASE)
    time.sleep(1.2)
    lab.src(CHANGES)
    ok(lab.replicate(spec))
    rows = lab.iceberg("snap").rows
    old = [dict(r) for r in rows if r["_sling_loaded_at"] == stamp1]
    new = [dict(r) for r in rows if r["_sling_loaded_at"] != stamp1]
    assert len({r["_sling_loaded_at"] for r in new}) == 1 and new[0]["_sling_loaded_at"] > stamp1
    for r in old + new:
        del r["_sling_loaded_at"]
    assert dict_rows(old) == dict_rows(BASE)
    assert dict_rows(new) == dict_rows([
        order(1, "ann2", Decimal("10.5"), ts(2024, 2, 1)), order(2, "bob2", Decimal("20"), ts(2024, 1, 2, 10)),
        order(3, "cy", None, ts(2024, 1, 3, 10)), order(4, "dee", Decimal("4"), ts(2024, 2, 2))])


def test_merge_ignores_added_columns_and_nulls_dropped_ones(lab):
    create_orders(lab)
    spec = stream(lab, "evo", "incremental", "    primary_key: [id]\n    update_key: updated_at\n"
                                             "    target_options:\n      add_new_columns: true\n")
    ok(lab.replicate(spec))
    lab.src("alter table sales.orders add column region text; update sales.orders set region = 'eu', updated_at = '2024-03-01' where id = 3")
    ok(lab.replicate(spec))
    table = lab.iceberg("evo")
    assert table.fields == ORDER_FIELDS
    assert dict_rows(table.rows) == dict_rows(BASE[:2] + [order(3, "cy", None, ts(2024, 3, 1))])
    lab.src("alter table sales.orders drop column amount; update sales.orders set updated_at = '2024-04-01' where id = 2")
    ok(lab.replicate(spec))
    table = lab.iceberg("evo")
    assert table.fields == ORDER_FIELDS
    assert dict_rows(table.rows) == dict_rows([
        BASE[0], order(2, "bob", None, ts(2024, 4, 1)), order(3, "cy", None, ts(2024, 3, 1))])


def test_append_with_a_new_source_column_fails_and_leaves_table(lab):
    create_orders(lab)
    spec = stream(lab, "app", "incremental", "    update_key: updated_at\n")
    ok(lab.replicate(spec))
    before = lab.metadata_location("app")
    lab.src("alter table sales.orders add column region text; update sales.orders set updated_at = '2024-03-01' where id = 1")
    failed(lab.replicate(spec))
    assert lab.metadata_location("app") == before
    table = lab.iceberg("app")
    assert table.fields == ORDER_FIELDS
    assert dict_rows(table.rows) == dict_rows(BASE)


def test_seed_then_merge_into_the_same_table(lab):
    # Adapted from the upstream iceberg incremental-merge replication test.
    create_orders(lab)
    spec = f"""
source: SRC
target: LAKE
streams:
  seed:
    sql: select * from sales.orders
    object: '{lab.glue_db}.m126'
    mode: full-refresh
    primary_key: [id]
  merge:
    sql: select * from sales.orders where {{incremental_where_cond}}
    object: '{lab.glue_db}.m126'
    mode: incremental
    primary_key: [id]
    update_key: updated_at
"""
    ok(lab.replicate(spec, "--streams", "seed"))
    assert dict_rows(lab.iceberg("m126").rows) == dict_rows(BASE)
    lab.src("update sales.orders set customer = 'a2', updated_at = '2024-02-01' where id = 1;"
            " update sales.orders set customer = 'b2', updated_at = '2024-02-01' where id = 2;"
            " insert into sales.orders values (6, 'd', 6, '2024-02-02')")
    ok(lab.replicate(spec, "--streams", "merge"))
    rows = lab.iceberg("m126").rows
    assert len(rows) == 4 and len({r["id"] for r in rows}) == 4
    assert dict_rows(rows) == dict_rows([
        order(1, "a2", Decimal("10.5"), ts(2024, 2, 1)), order(2, "b2", Decimal("20"), ts(2024, 2, 1)),
        order(3, "cy", None, ts(2024, 1, 3, 10)), order(6, "d", Decimal("6"), ts(2024, 2, 2))])


def test_column_casing_and_templated_names(lab):
    lab.src('create schema sales; create table sales.items (id int, "ItemName" text, "itemCount" int);'
            " insert into sales.items values (1, 'pen', 2)")
    ok(lab.replicate(f"""
source: SRC
target: LAKE
defaults:
  object: '{lab.glue_db}.{{stream_schema}}_{{stream_table}}'
streams:
  sales.items:
    target_options:
      column_casing: snake
  raw_items:
    sql: select * from sales.items
    object: '{lab.glue_db}_raw.{{stream_name}}'
"""))
    assert lab.glue_tables() == ["sales_items"]
    assert lab.glue_tables(lab.glue_db + "_raw") == ["raw_items"]
    snake = lab.iceberg("sales_items")
    assert snake.fields == [("id", "int"), ("item_name", "string"), ("item_count", "int")]
    assert snake.rows == [{"id": 1, "item_name": "pen", "item_count": 2}]
    raw = lab.iceberg("raw_items", lab.glue_db + "_raw")
    assert raw.fields == [("id", "int"), ("ItemName", "string"), ("itemCount", "int")]
    assert raw.rows == [{"id": 1, "ItemName": "pen", "itemCount": 2}]
