"""Replications into S3 files: CSV and JSON-lines rendering, file naming and splitting, and which
existing objects a write replaces. Expectations observed from the reference (see curation.md)."""

import csv
import io
import time

from synclab import (create_orders, create_typed, csv_records, csv_table, failed, gunzip, jsonl_records, ok,
                     same_json_multiset)

NOTES_DDL = ("create schema sales; create table sales.orders (id int primary key, customer text, amount numeric(10,2),"
             " updated_at timestamp, note text, flag boolean, j jsonb)")
NOTES_ROWS = ("insert into sales.orders values (1,'ann',10.50,'2024-01-01 10:00','plain',true,'{\"k\":1}'),"
              "(2,'bob, jr',20.00,'2024-01-02 10:00','has \"quotes\"',false,'[1,2]'),"
              "(3,'cy',null,'2024-01-03 10:00',E'multi\\nline',null,null),"
              "(4,'dee',4,'2024-01-04 10:00','',true,'\"s\"'),(5,'eve',5.5,'2024-01-05 10:00',null,false,'{}')")
IDS = "select id, customer from sales.orders order by id"
FIVE = [("1", "ann"), ("2", "bob, jr"), ("3", "cy"), ("4", "dee"), ("5", "eve")]


def notes(lab):
    lab.src(NOTES_DDL)
    lab.src(NOTES_ROWS)


def files(lab, body):
    return f"""
source: SRC
target: BUCKET
defaults:
  mode: full-refresh
streams:
{body}"""


def test_csv_rendering_of_postgres_types(lab):
    create_typed(lab)
    ok(lab.replicate(files(lab, "  sales.typed:\n    object: 'out/typed.csv'\n")))
    objs = lab.objects()
    assert list(objs) == ["out/typed.csv"]
    header, rows = csv_table(objs["out/typed.csv"])
    assert header == ["id", "s", "b", "n", "r", "d", "flag", "t", "v", "ch", "dt", "ts", "tz", "j", "js", "u", "tm", "by"]
    assert rows == [
        ("1", "7", "9007199254740993", "10.50", "1.5", "2.25", "true", 'he said "hi", ok', "v1", "ab ", "2024-03-04",
         "2024-01-02 10:00:00.123456 +00", "2024-01-02 08:00:00 +00", '{"a": [1, 2], "b": null}', '{"x":1}',
         "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11", "13:14:15", "\x01\x02"),
        ("2", "", "", "", "", "", "false", "line1\nline2", "", "", "", "", "", "", "", "", "", ""),
    ]


def test_jsonlines_rendering_of_postgres_types(lab):
    create_typed(lab)
    ok(lab.replicate(files(lab, "  typed_json:\n    sql: select * from sales.typed\n    object: 'out/typed.jsonl'\n")))
    objs = lab.objects()
    assert list(objs) == ["out/typed.jsonl"]
    records = jsonl_records(objs["out/typed.jsonl"])
    assert same_json_multiset(records, [
        {"id": 1, "s": 7, "b": 9007199254740993, "n": "10.50", "r": 1.5, "d": 2.25, "flag": "true",
         "t": 'he said "hi", ok', "v": "v1", "ch": "ab ", "dt": "2024-03-04T00:00:00Z",
         "ts": "2024-01-02T10:00:00.123456Z", "tz": "2024-01-02T08:00:00Z", "j": {"a": [1, 2], "b": None},
         "js": {"x": 1}, "u": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11", "tm": "13:14:15", "by": "\x01\x02"},
        {"id": 2, "s": None, "b": None, "n": None, "r": None, "d": None, "flag": "false", "t": "line1\nline2",
         "v": None, "ch": None, "dt": None, "ts": None, "tz": None, "j": None, "js": None, "u": None, "tm": None,
         "by": None},
    ]), records


def test_csv_quoting_nulls_and_json_cells(lab):
    notes(lab)
    ok(lab.replicate(files(lab, "  sales.orders:\n    object: 'a/{stream_schema}/{stream_table}.csv'\n")))
    objs = lab.objects()
    assert list(objs) == ["a/sales/orders.csv"]
    header, rows = csv_table(objs["a/sales/orders.csv"])
    assert header == ["id", "customer", "amount", "updated_at", "note", "flag", "j"]
    assert rows == [
        ("1", "ann", "10.50", "2024-01-01 10:00:00 +00", "plain", "true", '{"k": 1}'),
        ("2", "bob, jr", "20.00", "2024-01-02 10:00:00 +00", 'has "quotes"', "false", "[1, 2]"),
        ("3", "cy", "", "2024-01-03 10:00:00 +00", "multi\nline", "", ""),
        ("4", "dee", "4.00", "2024-01-04 10:00:00 +00", "", "true", '"s"'),
        ("5", "eve", "5.50", "2024-01-05 10:00:00 +00", "", "false", "{}"),
    ]


def test_jsonlines_nested_json_and_nulls(lab):
    notes(lab)
    ok(lab.replicate(files(lab, "  all_json:\n    sql: select * from sales.orders\n    object: 'e/one.jsonl'\n")))
    objs = lab.objects()
    assert list(objs) == ["e/one.jsonl"]
    records = jsonl_records(objs["e/one.jsonl"])
    expected = [
        {"id": 1, "customer": "ann", "amount": "10.50", "updated_at": "2024-01-01T10:00:00Z", "note": "plain",
         "flag": "true", "j": {"k": 1}},
        {"id": 2, "customer": "bob, jr", "amount": "20.00", "updated_at": "2024-01-02T10:00:00Z",
         "note": 'has "quotes"', "flag": "false", "j": [1, 2]},
        {"id": 3, "customer": "cy", "amount": None, "updated_at": "2024-01-03T10:00:00Z", "note": "multi\nline",
         "flag": None, "j": None},
        {"id": 4, "customer": "dee", "amount": "4.00", "updated_at": "2024-01-04T10:00:00Z", "note": "",
         "flag": "true", "j": "s"},
        {"id": 5, "customer": "eve", "amount": "5.50", "updated_at": "2024-01-05T10:00:00Z", "note": None,
         "flag": "false", "j": {}},
    ]
    assert same_json_multiset(records, expected), records


def test_header_and_delimiter_options(lab):
    notes(lab)
    ok(lab.replicate(files(lab, f"""  no_header:
    sql: {IDS}
    object: 'b/nohdr.csv'
    target_options:
      header: false
      delimiter: '|'
""")))
    objs = lab.objects()
    assert list(objs) == ["b/nohdr.csv"]
    records = list(csv.reader(io.StringIO(objs["b/nohdr.csv"].decode(), newline=""), delimiter="|"))
    assert sorted(tuple(r) for r in records) == FIVE


def test_file_max_rows_splits_into_numbered_parts(lab):
    notes(lab)
    ok(lab.replicate(files(lab, f"""  split_csv:
    sql: {IDS}
    object: 'c/split/'
    target_options:
      format: csv
      file_max_rows: 2
  split_json:
    sql: {IDS}
    object: 'd/split/'
    target_options:
      format: jsonlines
      file_max_rows: 2
  split_ext:
    sql: {IDS}
    object: 'q/data.csv'
    target_options:
      file_max_rows: 3
""")))
    objs = lab.objects()
    assert sorted(objs) == [
        "c/split/part.01.0001.csv", "c/split/part.01.0002.csv", "c/split/part.01.0003.csv",
        "d/split/part.01.0001.jsonl", "d/split/part.01.0002.jsonl", "d/split/part.01.0003.jsonl",
        "q/data.csv/part.01.0001.csv", "q/data.csv/part.01.0002.csv"]
    csv_parts = [csv_records(objs[f"c/split/part.01.000{i}.csv"]) for i in (1, 2, 3)]
    assert all(p[0] == ["id", "customer"] for p in csv_parts)
    assert [len(p) - 1 for p in csv_parts] == [2, 2, 1]
    assert sorted(tuple(r) for p in csv_parts for r in p[1:]) == FIVE
    json_parts = [jsonl_records(objs[f"d/split/part.01.000{i}.jsonl"]) for i in (1, 2, 3)]
    assert [len(p) for p in json_parts] == [2, 2, 1]
    assert same_json_multiset([r for p in json_parts for r in p],
                              [{"id": int(i), "customer": c} for i, c in FIVE])
    ext_parts = [csv_records(objs[f"q/data.csv/part.01.000{i}.csv"]) for i in (1, 2)]
    assert [len(p) - 1 for p in ext_parts] == [3, 2]
    assert sorted(tuple(r) for p in ext_parts for r in p[1:]) == FIVE


def test_exact_multiple_of_file_max_rows_leaves_a_trailing_empty_part(lab):
    notes(lab)
    ok(lab.replicate(files(lab, """  pairs:
    sql: select id, customer from sales.orders where id <= 4 order by id
    object: 't/pairs/'
    target_options:
      format: jsonlines
      file_max_rows: 2
  singles:
    sql: select id, customer from sales.orders where id <= 2 order by id
    object: 't/singles.csv'
    target_options:
      header: false
      file_max_rows: 1
""")))
    objs = lab.objects()
    assert sorted(objs) == ["t/pairs/part.01.0001.jsonl", "t/pairs/part.01.0002.jsonl", "t/pairs/part.01.0003.jsonl",
                            "t/singles.csv/part.01.0001.csv", "t/singles.csv/part.01.0002.csv",
                            "t/singles.csv/part.01.0003.csv"]
    assert objs["t/pairs/part.01.0003.jsonl"] == b"" and objs["t/singles.csv/part.01.0003.csv"] == b""
    singles = [csv_records(objs[f"t/singles.csv/part.01.000{i}.csv"]) for i in (1, 2)]
    assert [len(p) for p in singles] == [1, 1]
    assert sorted(tuple(r) for p in singles for r in p) == FIVE[:2]
    pairs = [jsonl_records(objs[f"t/pairs/part.01.000{i}.jsonl"]) for i in (1, 2)]
    assert [len(p) for p in pairs] == [2, 2]
    assert same_json_multiset([r for p in pairs for r in p], [{"id": int(i), "customer": c} for i, c in FIVE[:4]])


def test_format_and_compression_choose_the_written_key(lab):
    notes(lab)
    ok(lab.replicate(files(lab, f"""  plain:
    sql: select id from sales.orders where id < 3
    object: 'h/noext'
  as_json:
    sql: {IDS}
    object: 's/x'
    target_options:
      format: jsonlines
  zipped:
    sql: {IDS}
    object: 'f/gz.csv'
    target_options:
      compression: gzip
  by_url:
    sql: select id from sales.orders where id = 1
    object: 's3://{lab.bucket}/i/url.csv'
""")))
    objs = lab.objects()
    assert sorted(objs) == ["f/gz.csv.gz", "h/noext", "i/url.csv", "s/x"]
    assert csv_table(objs["h/noext"]) == (["id"], [("1",), ("2",)])
    assert same_json_multiset(jsonl_records(objs["s/x"]), [{"id": int(i), "customer": c} for i, c in FIVE])
    assert csv_table(gunzip(objs["f/gz.csv.gz"])) == (["id", "customer"], FIVE)
    assert csv_table(objs["i/url.csv"]) == (["id"], [("1",)])


def test_write_replaces_every_object_under_the_target_path(lab):
    create_orders(lab)
    for key in ["p/one.csv.bak", "p/one.csvx/y", "p/one.cs", "p/other.csv", "p/one.csv/inner",
                "q/dir/a", "q/dirx/b", "q/di", "q/dir/sub/c", "r/z.csv", "r/z.csv.gz.old", "r/z.csvq", "r/y.csv"]:
        lab.put(key)
    run = ("run", "--src-conn", "SRC", "--src-stream", "sales.orders", "--tgt-conn", "BUCKET", "--tgt-object")
    ok(lab.run(*run, "p/one.csv"))
    assert sorted(lab.objects("p/")) == ["p/one.cs", "p/one.csv", "p/other.csv"]
    assert sorted(lab.objects("q/")) == ["q/di", "q/dir/a", "q/dir/sub/c", "q/dirx/b"]
    ok(lab.run(*run, "q/dir/", "--tgt-options", '{"format": "csv", "file_max_rows": 2}'))
    assert sorted(lab.objects("q/")) == ["q/di", "q/dir/part.01.0001.csv", "q/dir/part.01.0002.csv"]
    ok(lab.run(*run, "r/z.csv", "--tgt-options", '{"compression": "gzip"}'))
    assert sorted(lab.objects("r/")) == ["r/y.csv", "r/z.csv.gz"]
    assert sorted(lab.objects("p/")) == ["p/one.cs", "p/one.csv", "p/other.csv"]


def test_rewrite_with_fewer_rows_drops_stale_parts(lab):
    notes(lab)
    spec = files(lab, f"""  split:
    sql: {IDS}
    object: 'c/split/'
    target_options:
      format: csv
      file_max_rows: 2
""")
    ok(lab.replicate(spec))
    assert len(lab.objects("c/")) == 3
    lab.src("delete from sales.orders where id > 1")
    ok(lab.replicate(spec))
    objs = lab.objects()
    assert list(objs) == ["c/split/part.01.0001.csv"]
    assert csv_table(objs["c/split/part.01.0001.csv"]) == (["id", "customer"], [("1", "ann")])


def test_empty_result_writes_and_removes_nothing(lab):
    create_orders(lab)
    lab.put("u/e.csv", b"old")
    lab.put("u/e.csv.x")
    ok(lab.run("run", "--src-conn", "SRC", "--src-stream", "select id from sales.orders where id > 100",
               "--tgt-conn", "BUCKET", "--tgt-object", "u/e.csv"))
    ok(lab.run("run", "--src-conn", "SRC", "--src-stream", "select id from sales.orders where id > 100",
               "--tgt-conn", "BUCKET", "--tgt-object", "u/new.csv"))
    assert lab.objects() == {"u/e.csv": b"old", "u/e.csv.x": b"sentinel"}


def test_snapshot_and_truncate_modes_on_files(lab):
    create_orders(lab, "(1,'ann',10.50,'2024-01-01 10:00')")
    start = int(time.time())
    ok(lab.run("run", "--src-conn", "SRC", "--src-stream", "sales.orders", "--tgt-conn", "BUCKET",
               "--tgt-object", "m/snap.csv", "--mode", "snapshot"))
    end = int(time.time())
    ok(lab.run("run", "--src-conn", "SRC", "--src-stream", "sales.orders", "--tgt-conn", "BUCKET",
               "--tgt-object", "n/tr.csv", "--mode", "truncate"))
    objs = lab.objects()
    assert sorted(objs) == ["m/snap.csv", "n/tr.csv"]
    header, rows = csv_table(objs["m/snap.csv"])
    assert header == ["id", "customer", "amount", "updated_at", "_sling_loaded_at"]
    assert len(rows) == 1 and rows[0][:4] == ("1", "ann", "10.50", "2024-01-01 10:00:00 +00")
    assert rows[0][4].isdigit() and start - 1 <= int(rows[0][4]) <= end + 1
    assert csv_table(objs["n/tr.csv"]) == (["id", "customer", "amount", "updated_at"],
                                           [("1", "ann", "10.50", "2024-01-01 10:00:00 +00")])


def test_incremental_mode_is_rejected_for_files(lab):
    create_orders(lab)
    lab.put("l/inc.csv", b"old")
    failed(lab.run("run", "--src-conn", "SRC", "--src-stream", "sales.orders", "--tgt-conn", "BUCKET",
                   "--tgt-object", "l/inc.csv", "--mode", "incremental", "--update-key", "updated_at"))
    assert lab.objects() == {"l/inc.csv": b"old"}
