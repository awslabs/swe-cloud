"""Multi-stream replications and multi-run cycles: defaults and overrides, wildcard expansion,
whole-file validation failures, and one source kept in sync with all three target kinds."""

import datetime as dt
from decimal import Decimal

from synclab import create_orders, csv_table, dict_rows, failed, jsonl_records, ok, same_json_multiset

UTC = dt.timezone.utc


def test_defaults_merge_with_wildcard_and_explicit_streams(lab):
    create_orders(lab)
    lab.src("create table sales.ord_items (id int, qty int); insert into sales.ord_items values (1, 5);"
            " create table sales.order_log (id int, msg text); insert into sales.order_log values (1, 'x');"
            " create table sales.customers (id int); insert into sales.customers values (1)")
    ok(lab.replicate("""
source: SRC
target: TGT
defaults:
  mode: full-refresh
  object: 'dm.{stream_table_upper}'
  target_options:
    column_casing: upper
streams:
  sales.ord*:
    target_options:
      table_keys:
        primary: [ID]
  sales.orders:
    object: 'dm.{stream_schema_upper}_{stream_table_lower}'
    select: [id, customer]
"""))
    assert lab.tgt_tables("dm") == ["ORDER_LOG", "ORD_ITEMS", "SALES_orders"]
    assert [c[0] for c in lab.tgt_columns("dm", "ORD_ITEMS")] == ["ID", "QTY"]
    assert [c[0] for c in lab.tgt_columns("dm", "ORDER_LOG")] == ["ID", "MSG"]
    assert [c[0] for c in lab.tgt_columns("dm", "SALES_orders")] == ["ID", "CUSTOMER"]
    assert lab.tgt_rows('dm."SALES_orders"') == [(1, "ann"), (2, "bob"), (3, "cy")]
    keyed = lab.tgt("select conrelid::regclass::text from pg_constraint where contype = 'p'"
                    " and connamespace = 'dm'::regnamespace order by 1")
    assert [r[0] for r in keyed] == ['dm."ORDER_LOG"', 'dm."ORD_ITEMS"']


def test_wildcard_expansion_keeps_source_table_case(lab):
    lab.src('create schema sales; create table sales."LineItems" (id int, "ItemName" text);'
            " insert into sales.\"LineItems\" values (1, 'pen'), (2, 'ink');"
            " create table sales.customers (id int, name text); insert into sales.customers values (1, 'ann');"
            " create schema other; create table other.ignored (id int); insert into other.ignored values (1)")
    ok(lab.replicate("""
source: SRC
target: TGT
defaults:
  object: 'wc.{stream_schema}_{stream_table}'
streams:
  sales.*:
"""))
    assert lab.tgt_tables("wc") == ["sales_LineItems", "sales_customers"]
    assert lab.tgt_rows('wc."sales_LineItems"') == [(1, "pen"), (2, "ink")]
    ok(lab.replicate("""
source: SRC
target: BUCKET
defaults:
  object: 'exports/{stream_table}.csv'
  target_options:
    column_casing: snake
streams:
  sales.*:
"""))
    objs = lab.objects()
    assert sorted(objs) == ["exports/LineItems.csv", "exports/customers.csv"]
    assert csv_table(objs["exports/LineItems.csv"]) == (["id", "item_name"], [("1", "pen"), ("2", "ink")])
    assert csv_table(objs["exports/customers.csv"]) == (["id", "name"], [("1", "ann")])


def test_unsupported_stream_mode_stops_the_whole_replication(lab):
    create_orders(lab)
    lab.src("create table sales.ord_items (id int); insert into sales.ord_items values (1);"
            " create table sales.order_log (id int); insert into sales.order_log values (1)")
    failed(lab.replicate(f"""
source: SRC
target: LAKE
defaults:
  object: '{lab.glue_db}.{{stream_table}}'
streams:
  sales.ord_items:
  sales.order_log:
    mode: truncate
  sales.orders:
"""))
    assert lab.glue_tables() in (None, [])


def test_one_source_kept_in_sync_with_every_target(lab):
    create_orders(lab)
    lake = f"""
source: SRC
target: LAKE
defaults:
  mode: incremental
  primary_key: [id]
  update_key: updated_at
  object: '{lab.glue_db}.{{stream_schema}}_{{stream_table}}'
streams:
  sales.orders:
"""
    mirror = """
source: SRC
target: TGT
defaults:
  object: 'mirror.{stream_schema}_{stream_table}'
streams:
  sales.orders:
    mode: incremental
    primary_key: [id]
    update_key: updated_at
  sales.orders_snap:
    sql: select id, customer from sales.orders
    mode: snapshot
"""
    exports = """
source: SRC
target: BUCKET
defaults:
  mode: full-refresh
streams:
  sales.orders:
    object: 'exports/{stream_schema}/{stream_table}.csv'
  orders_json:
    sql: select id, customer from sales.orders order by id
    object: 'exports/json/{stream_name}/'
    target_options:
      format: jsonlines
      file_max_rows: 2
"""
    for spec in (lake, mirror, exports):
        ok(lab.replicate(spec))
    first_location = lab.metadata_location("sales_orders")
    assert lab.tgt_rows("mirror.sales_orders", "id, customer") == [(1, "ann"), (2, "bob"), (3, "cy")]
    assert len(lab.tgt_rows("mirror.sales_orders_snap")) == 3

    lab.src("update sales.orders set customer = 'ann2', updated_at = '2024-02-01' where id = 1;"
            " insert into sales.orders values (4, 'dee', 4.00, '2024-02-02');"
            " insert into sales.orders values (5, 'eve', 5.00, '2023-12-31')")
    for spec in (lake, mirror, exports):
        ok(lab.replicate(spec))

    assert lab.metadata_location("sales_orders") != first_location
    lake_rows = lab.iceberg("sales_orders").rows
    assert dict_rows(lake_rows) == dict_rows([
        {"id": 1, "customer": "ann2", "amount": Decimal("10.5"), "updated_at": dt.datetime(2024, 2, 1, tzinfo=UTC)},
        {"id": 2, "customer": "bob", "amount": Decimal("20"), "updated_at": dt.datetime(2024, 1, 2, 10, tzinfo=UTC)},
        {"id": 3, "customer": "cy", "amount": None, "updated_at": dt.datetime(2024, 1, 3, 10, tzinfo=UTC)},
        {"id": 4, "customer": "dee", "amount": Decimal("4"), "updated_at": dt.datetime(2024, 2, 2, tzinfo=UTC)},
    ])
    assert lab.tgt_rows("mirror.sales_orders") == [
        (1, "ann2", Decimal("10.50"), dt.datetime(2024, 2, 1)), (2, "bob", Decimal("20.00"), dt.datetime(2024, 1, 2, 10)),
        (3, "cy", None, dt.datetime(2024, 1, 3, 10)), (4, "dee", Decimal("4.00"), dt.datetime(2024, 2, 2))]
    snap = lab.tgt_rows("mirror.sales_orders_snap", "id, customer")
    assert snap == [(1, "ann"), (1, "ann2"), (2, "bob"), (2, "bob"), (3, "cy"), (3, "cy"), (4, "dee"), (5, "eve")]

    objs = lab.objects()
    assert sorted(objs) == ["exports/json/orders_json/part.01.0001.jsonl", "exports/json/orders_json/part.01.0002.jsonl",
                            "exports/json/orders_json/part.01.0003.jsonl", "exports/sales/orders.csv"]
    assert csv_table(objs["exports/sales/orders.csv"]) == (["id", "customer", "amount", "updated_at"], [
        ("1", "ann2", "10.50", "2024-02-01 00:00:00 +00"), ("2", "bob", "20.00", "2024-01-02 10:00:00 +00"),
        ("3", "cy", "", "2024-01-03 10:00:00 +00"), ("4", "dee", "4.00", "2024-02-02 00:00:00 +00"),
        ("5", "eve", "5.00", "2023-12-31 00:00:00 +00")])
    parts = [jsonl_records(objs[f"exports/json/orders_json/part.01.000{i}.jsonl"]) for i in (1, 2, 3)]
    assert [len(p) for p in parts] == [2, 2, 1]
    assert same_json_multiset([r for p in parts for r in p], [
        {"id": 1, "customer": "ann2"}, {"id": 2, "customer": "bob"}, {"id": 3, "customer": "cy"},
        {"id": 4, "customer": "dee"}, {"id": 5, "customer": "eve"}])


def test_default_column_casing_across_targets(lab):
    # No column_casing anywhere: the program's default naming applies to every target kind.
    lab.src('create schema sales; create table sales.caps ("ID" int, "UPPER_COL" text, "MixedCol" text,'
            ' lower_col text, "Has Space" text); insert into sales.caps values (1, \'A\', \'m\', \'l\', \'s\')')
    ok(lab.replicate("""
source: SRC
target: TGT
streams:
  sales.caps:
    object: 'dc.caps'
"""))
    assert [c[0] for c in lab.tgt_columns("dc", "caps")] == ["id", "upper_col", "MixedCol", "lower_col", "Has Space"]
    assert lab.tgt_rows("dc.caps") == [(1, "A", "m", "l", "s")]
    ok(lab.replicate(f"""
source: SRC
target: LAKE
streams:
  sales.caps:
    object: '{lab.glue_db}.caps'
"""))
    table = lab.iceberg("caps")
    assert table.columns == ["id", "upper_col", "MixedCol", "lower_col", "Has Space"]
    assert table.rows == [{"id": 1, "upper_col": "A", "MixedCol": "m", "lower_col": "l", "Has Space": "s"}]
    ok(lab.replicate("""
source: SRC
target: BUCKET
streams:
  sales.caps:
    object: 'dc/caps.csv'
  caps_json:
    sql: select * from sales.caps
    object: 'dc/caps.jsonl'
"""))
    objs = lab.objects()
    assert sorted(objs) == ["dc/caps.csv", "dc/caps.jsonl"]
    assert csv_table(objs["dc/caps.csv"]) == (["id", "upper_col", "MixedCol", "lower_col", "Has_Space"],
                                              [("1", "A", "m", "l", "s")])
    assert same_json_multiset(jsonl_records(objs["dc/caps.jsonl"]), [
        {"id": 1, "upper_col": "A", "MixedCol": "m", "lower_col": "l", "Has Space": "s"}])
