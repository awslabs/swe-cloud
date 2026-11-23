"""Single replications into a PostgreSQL target: table shape, typing and how each mode treats
existing target rows. Expectations were observed from the reference (see curation.md)."""

import datetime as dt
import time
import uuid
from decimal import Decimal

import pytest

from synclab import create_orders, create_typed, failed, ok, sorted_rows

UTC = dt.timezone.utc


def stream(obj, mode, extra=""):
    return f"""
source: SRC
target: TGT
streams:
  sales.orders:
    object: '{obj}'
    mode: {mode}
{extra}"""


def test_type_mapping_and_values(lab):
    create_typed(lab)
    ok(lab.replicate("""
source: SRC
target: TGT
defaults:
  mode: full-refresh
  object: 'mirror.{stream_table}'
streams:
  sales.typed:
"""))
    assert lab.tgt_columns("mirror", "typed") == [
        ("id", "integer", None, None, None),
        ("s", "smallint", None, None, None),
        ("b", "bigint", None, None, None),
        ("n", "numeric", None, None, None),
        ("r", "double precision", None, None, None),
        ("d", "double precision", None, None, None),
        ("flag", "boolean", None, None, None),
        ("t", "text", None, None, None),
        ("v", "character varying", 20, None, None),
        ("ch", "character varying", 3, None, None),
        ("dt", "date", None, None, None),
        ("ts", "timestamp without time zone", None, None, None),
        ("tz", "timestamp with time zone", None, None, None),
        ("j", "jsonb", None, None, None),
        ("js", "jsonb", None, None, None),
        ("u", "uuid", None, None, None),
        ("tm", "time without time zone", None, None, None),
        ("by", "bytea", None, None, None),
    ]
    rows = lab.tgt_rows("mirror.typed")
    assert rows == [
        (1, 7, 9007199254740993, Decimal("10.50"), 1.5, 2.25, True, 'he said "hi", ok', "v1", "ab ",
         dt.date(2024, 3, 4), dt.datetime(2024, 1, 2, 10, 0, 0, 123456), dt.datetime(2024, 1, 2, 8, 0, tzinfo=UTC),
         {"a": [1, 2], "b": None}, {"x": 1}, uuid.UUID("a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"),
         dt.time(13, 14, 15), b"\x01\x02"),
        (2, None, None, None, None, None, False, "line1\nline2", None, None, None, None, None, None, None, None,
         None, None),
    ]
    assert type(rows[0][6]) is bool and type(rows[1][6]) is bool


def test_full_refresh_recreates_the_table(lab):
    create_orders(lab)
    ok(lab.replicate(stream("mirror.fr", "full-refresh")))
    lab.tgt("alter table mirror.fr add column extra text default 'x';"
            " insert into mirror.fr (id, customer) values (99, 'stale')")
    lab.src("update sales.orders set customer = 'ann2' where id = 1; delete from sales.orders where id = 3")
    ok(lab.replicate(stream("mirror.fr", "full-refresh")))
    assert lab.tgt_columns("mirror", "fr") == [
        ("id", "integer", None, None, None), ("customer", "text", None, None, None),
        ("amount", "numeric", None, None, None), ("updated_at", "timestamp without time zone", None, None, None)]
    assert lab.tgt_rows("mirror.fr") == [
        (1, "ann2", Decimal("10.50"), dt.datetime(2024, 1, 1, 10)),
        (2, "bob", Decimal("20.00"), dt.datetime(2024, 1, 2, 10)),
    ]


def test_truncate_keeps_existing_definition_and_adds_missing_columns(lab):
    create_orders(lab)
    lab.tgt("create schema mirror; create table mirror.tr (id bigint, customer varchar(50), legacy text default 'L');"
            " insert into mirror.tr values (99, 'stale', 'old')")
    lab.tgt("create table mirror.other (id int); insert into mirror.other values (7)")
    ok(lab.replicate(stream("mirror.tr", "truncate")))
    assert lab.tgt_columns("mirror", "tr") == [
        ("id", "bigint", None, None, None), ("customer", "character varying", 50, None, None),
        ("legacy", "text", None, None, None), ("amount", "numeric", None, None, None),
        ("updated_at", "timestamp without time zone", None, None, None)]
    assert lab.tgt_rows("mirror.tr") == [
        (1, "ann", "L", Decimal("10.50"), dt.datetime(2024, 1, 1, 10)),
        (2, "bob", "L", Decimal("20.00"), dt.datetime(2024, 1, 2, 10)),
        (3, "cy", "L", None, dt.datetime(2024, 1, 3, 10)),
    ]
    assert lab.tgt_rows("mirror.other") == [(7,)]
    # A second truncate run replaces the rows again rather than appending.
    lab.src("delete from sales.orders where id = 2")
    ok(lab.replicate(stream("mirror.tr", "truncate")))
    assert [r[:2] for r in lab.tgt_rows("mirror.tr")] == [(1, "ann"), (3, "cy")]


def test_truncate_creates_a_missing_table(lab):
    create_orders(lab)
    ok(lab.replicate(stream("fresh.tr", "truncate")))
    assert lab.tgt_columns("fresh", "tr") == [
        ("id", "integer", None, None, None), ("customer", "text", None, None, None),
        ("amount", "numeric", None, None, None), ("updated_at", "timestamp without time zone", None, None, None)]
    assert len(lab.tgt_rows("fresh.tr")) == 3


def test_incremental_update_key_only_appends_newer_rows(lab):
    create_orders(lab)
    spec = stream("mirror.inc_u", "incremental", "    update_key: updated_at\n")
    ok(lab.replicate(spec))
    assert len(lab.tgt_rows("mirror.inc_u")) == 3
    lab.src("update sales.orders set customer = 'ann2', updated_at = '2024-02-01' where id = 1;"
            " insert into sales.orders values (4, 'dee', 4, '2024-02-02');"
            " update sales.orders set customer = 'bob2' where id = 2")
    ok(lab.replicate(spec))
    assert lab.tgt_rows("mirror.inc_u") == [
        (1, "ann", Decimal("10.50"), dt.datetime(2024, 1, 1, 10)),
        (1, "ann2", Decimal("10.50"), dt.datetime(2024, 2, 1)),
        (2, "bob", Decimal("20.00"), dt.datetime(2024, 1, 2, 10)),
        (3, "cy", None, dt.datetime(2024, 1, 3, 10)),
        (4, "dee", Decimal("4.00"), dt.datetime(2024, 2, 2)),
    ]


def test_incremental_primary_key_only_upserts_every_row(lab):
    create_orders(lab)
    spec = stream("mirror.inc_p", "incremental", "    primary_key: [id]\n")
    ok(lab.replicate(spec))
    lab.src("update sales.orders set customer = 'ann2' where id = 1;"
            " insert into sales.orders values (4, 'dee', 4, '2023-01-01');"
            " delete from sales.orders where id = 2")
    ok(lab.replicate(spec))
    assert lab.tgt_rows("mirror.inc_p") == [
        (1, "ann2", Decimal("10.50"), dt.datetime(2024, 1, 1, 10)),
        (2, "bob", Decimal("20.00"), dt.datetime(2024, 1, 2, 10)),
        (3, "cy", None, dt.datetime(2024, 1, 3, 10)),
        (4, "dee", Decimal("4.00"), dt.datetime(2023, 1, 1)),
    ]


def test_incremental_merge_reads_only_rows_past_the_target_maximum(lab):
    create_orders(lab, "(1,'ann',10.50,'2024-01-01 10:00'),(2,'bob',20.00,'2024-01-02 10:00'),"
                       "(3,'cy',null,'2024-01-03 10:00'),(5,'nul',1,null)")
    spec = stream("mirror.inc", "incremental", "    primary_key: [id]\n    update_key: updated_at\n")
    ok(lab.replicate(spec))
    assert len(lab.tgt_rows("mirror.inc")) == 4
    # Changes whose update key does not pass the target maximum are not read again: the row at
    # exactly the maximum, a row with an older key and a row with a null key.
    lab.tgt("update mirror.inc set customer = 'tampered' where id = 3")
    lab.src("update sales.orders set customer = 'cy-same' where id = 3;"
            " update sales.orders set customer = 'bob-old' where id = 2;"
            " update sales.orders set customer = 'nul2' where id = 5")
    ok(lab.replicate(spec))
    assert lab.tgt_rows("mirror.inc", "id, customer") == [(1, "ann"), (2, "bob"), (3, "tampered"), (5, "nul")]
    lab.src("update sales.orders set customer = 'ann2', updated_at = '2024-02-01' where id = 1;"
            " insert into sales.orders values (4, 'dee', 4, '2024-02-02')")
    ok(lab.replicate(spec))
    assert lab.tgt_rows("mirror.inc") == [
        (1, "ann2", Decimal("10.50"), dt.datetime(2024, 2, 1)),
        (2, "bob", Decimal("20.00"), dt.datetime(2024, 1, 2, 10)),
        (3, "tampered", None, dt.datetime(2024, 1, 3, 10)),
        (4, "dee", Decimal("4.00"), dt.datetime(2024, 2, 2)),
        (5, "nul", Decimal("1.00"), None),
    ]


def test_incremental_high_water_mark_comes_from_existing_target(lab):
    create_orders(lab)
    lab.tgt("create schema mirror; create table mirror.pre (id int, customer text, amount numeric, updated_at timestamp);"
            " insert into mirror.pre values (100, 'seed', 0, '2024-01-02 10:00')")
    ok(lab.replicate(stream("mirror.pre", "incremental", "    update_key: updated_at\n")))
    assert lab.tgt_rows("mirror.pre") == [
        (3, "cy", None, dt.datetime(2024, 1, 3, 10)),
        (100, "seed", Decimal("0"), dt.datetime(2024, 1, 2, 10)),
    ]
    # Lowering the target's maximum makes the next run read the source from that point again.
    lab.tgt("delete from mirror.pre where id = 3")
    ok(lab.replicate(stream("mirror.pre", "incremental", "    update_key: updated_at\n")))
    assert lab.tgt_rows("mirror.pre", "id") == [(3,), (100,)]


def test_incremental_with_nothing_new_changes_nothing(lab):
    create_orders(lab)
    spec = stream("mirror.inc", "incremental", "    primary_key: [id]\n    update_key: updated_at\n")
    ok(lab.replicate(spec))
    before = lab.tgt_rows("mirror.inc")
    ok(lab.replicate(spec))
    assert lab.tgt_rows("mirror.inc") == before


def test_snapshot_appends_a_stamped_copy_each_run(lab):
    create_orders(lab)
    spec = stream("mirror.snap", "snapshot")
    start = int(time.time())
    ok(lab.replicate(spec))
    first_end = int(time.time())
    assert lab.tgt_columns("mirror", "snap") == [
        ("id", "integer", None, None, None), ("customer", "text", None, None, None),
        ("amount", "numeric", None, None, None), ("updated_at", "timestamp without time zone", None, None, None),
        ("_sling_loaded_at", "integer", None, None, None)]
    first = lab.tgt_rows("mirror.snap")
    assert [r[:4] for r in first] == [
        (1, "ann", Decimal("10.50"), dt.datetime(2024, 1, 1, 10)),
        (2, "bob", Decimal("20.00"), dt.datetime(2024, 1, 2, 10)),
        (3, "cy", None, dt.datetime(2024, 1, 3, 10)),
    ]
    stamps = {r[4] for r in first}
    assert len(stamps) == 1
    stamp1 = stamps.pop()
    assert type(stamp1) is int and start - 1 <= stamp1 <= first_end + 1
    time.sleep(1.2)
    lab.src("update sales.orders set customer = 'ann2' where id = 1; insert into sales.orders values (4,'dee',4,null)")
    ok(lab.replicate(spec))
    rows = lab.tgt_rows("mirror.snap")
    assert len(rows) == 7
    second = sorted_rows(r[:4] for r in rows if r[4] != stamp1)
    assert second == [
        (1, "ann2", Decimal("10.50"), dt.datetime(2024, 1, 1, 10)),
        (2, "bob", Decimal("20.00"), dt.datetime(2024, 1, 2, 10)),
        (3, "cy", None, dt.datetime(2024, 1, 3, 10)),
        (4, "dee", Decimal("4.00"), None),
    ]
    stamp2 = {r[4] for r in rows if r[4] != stamp1}
    assert len(stamp2) == 1 and stamp2.pop() > stamp1
    assert sorted_rows(r[:4] for r in rows if r[4] == stamp1) == [r[:4] for r in first]


CASING = {
    "source": ["id", "ItemName", "itemCount", "Unit Price", "lower_col", "UPPER_COL", "DHL OriginalTracking-Number"],
    "target": ["id", "itemname", "itemcount", "unit_price", "lower_col", "upper_col", "dhl_originaltracking_number"],
    "snake": ["id", "item_name", "item_count", "unit_price", "lower_col", "upper_col", "dhl_original_tracking_number"],
    "upper": ["ID", "ITEMNAME", "ITEMCOUNT", "UNIT_PRICE", "LOWER_COL", "UPPER_COL", "DHL_ORIGINALTRACKING_NUMBER"],
    "lower": ["id", "itemname", "itemcount", "unit_price", "lower_col", "upper_col", "dhl_originaltracking_number"],
    "normalize": ["id", "ItemName", "itemCount", "Unit Price", "lower_col", "upper_col", "DHL OriginalTracking-Number"],
}


@pytest.mark.parametrize("casing", sorted(CASING))
def test_column_casing(lab, casing):
    lab.src('create schema sales; create table sales."LineItems" (id int primary key, "ItemName" text, "itemCount" int,'
            ' "Unit Price" numeric(8,2), lower_col text, "UPPER_COL" text, "DHL OriginalTracking-Number" text)')
    lab.src("""insert into sales."LineItems" values (1,'pen',2,1.25,'a','B','T1'),(2,'ink',5,3.00,'c','D','T2')""")
    ok(lab.replicate(f"""
source: SRC
target: TGT
streams:
  sales.LineItems:
    object: 'cc.items'
    target_options:
      column_casing: {casing}
"""))
    names = CASING[casing]
    assert [c[0] for c in lab.tgt_columns("cc", "items")] == names
    assert lab.tgt_rows("cc.items", ", ".join('"%s"' % n for n in names)) == [
        (1, "pen", 2, Decimal("1.25"), "a", "B", "T1"), (2, "ink", 5, Decimal("3.00"), "c", "D", "T2")]


def test_new_source_columns_are_added_unless_disabled(lab):
    create_orders(lab)
    pk = "    primary_key: [id]\n    update_key: updated_at\n"
    ok(lab.replicate(stream("m.added", "incremental", pk)))
    ok(lab.replicate(stream("m.kept", "incremental", pk + "    target_options:\n      add_new_columns: false\n")))
    lab.src("alter table sales.orders add column region text default 'eu'; update sales.orders set updated_at = '2024-03-01' where id = 1")
    ok(lab.replicate(stream("m.added", "incremental", pk)))
    ok(lab.replicate(stream("m.kept", "incremental", pk + "    target_options:\n      add_new_columns: false\n")))
    assert [c[:2] for c in lab.tgt_columns("m", "added")] == [
        ("id", "integer"), ("customer", "text"), ("amount", "numeric"),
        ("updated_at", "timestamp without time zone"), ("region", "text")]
    assert lab.tgt_rows("m.added", "id, updated_at, region") == [
        (1, dt.datetime(2024, 3, 1), "eu"), (2, dt.datetime(2024, 1, 2, 10), None), (3, dt.datetime(2024, 1, 3, 10), None)]
    assert [c[0] for c in lab.tgt_columns("m", "kept")] == ["id", "customer", "amount", "updated_at"]
    assert lab.tgt_rows("m.kept", "id, updated_at") == [
        (1, dt.datetime(2024, 3, 1)), (2, dt.datetime(2024, 1, 2, 10)), (3, dt.datetime(2024, 1, 3, 10))]


def test_snapshot_and_append_targets_gain_new_columns(lab):
    create_orders(lab)
    ok(lab.replicate(stream("m.u", "incremental", "    update_key: updated_at\n")))
    lab.src("alter table sales.orders add column region text default 'eu'; update sales.orders set updated_at = '2024-03-01' where id = 2")
    ok(lab.replicate(stream("m.u", "incremental", "    update_key: updated_at\n")))
    assert [c[0] for c in lab.tgt_columns("m", "u")] == ["id", "customer", "amount", "updated_at", "region"]
    assert lab.tgt_rows("m.u", "id, region") == [(1, None), (2, None), (2, "eu"), (3, None)]


def test_merge_after_source_drops_a_column(lab):
    create_orders(lab)
    spec = stream("m.pu", "incremental", "    primary_key: [id]\n    update_key: updated_at\n")
    ok(lab.replicate(spec))
    lab.src("alter table sales.orders drop column amount;"
            " update sales.orders set updated_at = '2024-02-01', customer = 'ann2' where id = 1")
    ok(lab.replicate(spec))
    assert [c[0] for c in lab.tgt_columns("m", "pu")] == ["id", "customer", "amount", "updated_at"]
    # The merged row keeps its stored value for the column the source no longer has.
    assert lab.tgt_rows("m.pu") == [
        (1, "ann2", Decimal("10.50"), dt.datetime(2024, 2, 1)),
        (2, "bob", Decimal("20.00"), dt.datetime(2024, 1, 2, 10)),
        (3, "cy", None, dt.datetime(2024, 1, 3, 10)),
    ]


def test_values_too_wide_for_existing_columns_fail_without_changing_rows(lab):
    lab.src("create schema sales; create table sales.w (id int primary key, v varchar(5), n int, updated_at timestamp);"
            " insert into sales.w values (1, 'abc', 1, '2024-01-01'), (3, 'old', 3, '2024-01-01')")
    spec = """
source: SRC
target: TGT
streams:
  sales.w:
    object: 'm.w'
    mode: incremental
    primary_key: id
    update_key: updated_at
"""
    ok(lab.replicate(spec))
    lab.src("alter table sales.w alter column v type varchar(50); alter table sales.w alter column n type bigint;"
            " insert into sales.w values (2, 'abcdefghijkl', 9000000000, '2024-02-01');"
            " update sales.w set v = 'new', updated_at = '2024-02-01' where id = 3")
    failed(lab.replicate(spec))
    assert [c[:3] for c in lab.tgt_columns("m", "w")] == [
        ("id", "integer", None), ("v", "character varying", 5), ("n", "integer", None),
        ("updated_at", "timestamp without time zone", None)]
    assert lab.tgt_rows("m.w") == [(1, "abc", 1, dt.datetime(2024, 1, 1)), (3, "old", 3, dt.datetime(2024, 1, 1))]


def test_composite_primary_key_merge(lab):
    lab.src("create schema sales; create table sales.cp (a int, b text, v text, primary key (a, b));"
            " insert into sales.cp values (1,'x','one'),(1,'y','two'),(2,'x','three')")
    spec = """
source: SRC
target: TGT
streams:
  sales.cp:
    object: 'm.cp'
    mode: incremental
    primary_key: [a, b]
"""
    ok(lab.replicate(spec))
    lab.src("update sales.cp set v = 'TWO' where a = 1 and b = 'y'; insert into sales.cp values (2,'y','four')")
    ok(lab.replicate(spec))
    assert lab.tgt_rows("m.cp") == [(1, "x", "one"), (1, "y", "TWO"), (2, "x", "three"), (2, "y", "four")]


def test_custom_sql_with_incremental_condition(lab):
    create_orders(lab)
    spec = """
source: SRC
target: TGT
streams:
  recent:
    sql: select id, customer, updated_at from sales.orders where {incremental_where_cond}
    object: 'm.recent'
    mode: incremental
    primary_key: [id]
    update_key: updated_at
"""
    ok(lab.replicate(spec))
    lab.tgt("update m.recent set customer = 'tampered' where id = 2")
    lab.src("update sales.orders set customer = 'ann3', updated_at = '2024-06-01' where id = 1")
    ok(lab.replicate(spec))
    assert [c[0] for c in lab.tgt_columns("m", "recent")] == ["id", "customer", "updated_at"]
    assert lab.tgt_rows("m.recent") == [
        (1, "ann3", dt.datetime(2024, 6, 1)), (2, "tampered", dt.datetime(2024, 1, 2, 10)),
        (3, "cy", dt.datetime(2024, 1, 3, 10))]


def test_select_where_and_column_casts(lab):
    lab.src("create schema sales; create table sales.customers (id int primary key, name text, region text, updated_at timestamp);"
            " insert into sales.customers values (1,'ann','eu','2024-01-01'),(2,'bob','us','2024-01-02'),(3,'cy','eu','2024-01-03');"
            " create table sales.orders (id int, cust_id int, amount numeric(10,2));"
            " insert into sales.orders values (10,1,5),(11,2,6),(12,3,7)")
    ok(lab.replicate("""
source: SRC
target: TGT
streams:
  sales.customers:
    object: 'sw.c_excl'
    select: ['-region']
  eu_customers:
    sql: select id, name from sales.customers where region = 'eu'
    object: 'sw.c_custom'
    columns:
      id: bigint
  sales.orders:
    object: 'sw.o_where'
    select: [id, amount]
    where: amount > 5
"""))
    assert [c[:2] for c in lab.tgt_columns("sw", "c_excl")] == [
        ("id", "integer"), ("name", "text"), ("updated_at", "timestamp without time zone")]
    assert lab.tgt_rows("sw.c_excl") == [
        (1, "ann", dt.datetime(2024, 1, 1)), (2, "bob", dt.datetime(2024, 1, 2)), (3, "cy", dt.datetime(2024, 1, 3))]
    assert [c[:2] for c in lab.tgt_columns("sw", "c_custom")] == [("id", "bigint"), ("name", "text")]
    assert lab.tgt_rows("sw.c_custom") == [(1, "ann"), (3, "cy")]
    assert [c[:2] for c in lab.tgt_columns("sw", "o_where")] == [("id", "integer"), ("amount", "numeric")]
    assert lab.tgt_rows("sw.o_where") == [(11, Decimal("6.00")), (12, Decimal("7.00"))]


def test_table_keys_declare_a_primary_key(lab):
    create_orders(lab)
    ok(lab.replicate(stream("m.keyed", "full-refresh", "    target_options:\n      table_keys:\n        primary: [id]\n")))
    ok(lab.replicate(stream("m.plain", "full-refresh")))
    keyed = lab.tgt("select a.attname from pg_constraint c join pg_attribute a on a.attrelid = c.conrelid"
                    " and a.attnum = any(c.conkey) where c.conrelid = 'm.keyed'::regclass and c.contype = 'p'")
    assert [r[0] for r in keyed] == ["id"]
    assert lab.tgt("select count(*) from pg_constraint where conrelid = 'm.plain'::regclass and contype = 'p'") == [(0,)]


def test_empty_source_leaves_targets_alone(lab):
    lab.src("create schema sales; create table sales.empty (id int, v text)")
    lab.tgt("create schema m; create table m.empty (id int, v text); insert into m.empty values (9, 'keep')")
    ok(lab.run("run", "--src-conn", "SRC", "--src-stream", "sales.empty", "--tgt-conn", "TGT", "--tgt-object", "m.empty"))
    assert lab.tgt_rows("m.empty") == [(9, "keep")]
    ok(lab.run("run", "--src-conn", "SRC", "--src-stream", "sales.empty", "--tgt-conn", "TGT", "--tgt-object", "m.fresh"))
    assert lab.tgt_tables("m") == ["empty"]
