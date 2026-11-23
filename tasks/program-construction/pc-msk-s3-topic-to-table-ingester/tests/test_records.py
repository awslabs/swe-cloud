"""Row content of ingested messages: transforms, Kafka metadata, schema coercion, partition values."""

import datetime
from collections import Counter

import pytest
from lake import JsonText, array, assert_rows_by, kinds, quiet, struct, ts, txn_actions

ROW_FIELDS = [
    ("id", "integer"), ("name", "string"), ("score", "double"), ("active", "boolean"), ("ts", "timestamp"),
    ("tags", array("string")), ("meta", struct(("source", "string"), ("copied", "string"))), ("date", "string"),
    ("kafka_offset", "long"), ("kafka_partition", "integer"), ("kafka_topic", "string"), ("kafka_ts", "timestamp"),
    ("kafka_ts_type", "integer"), ("iso", "string"), ("micros", "long"), ("label", "string"),
]

TRANSFORMS = [
    "date: substr(ts, `0`, `10`)", "kafka_offset: kafka.offset", "kafka_partition: kafka.partition",
    "kafka_topic: kafka.topic", "kafka_ts: kafka.timestamp", "kafka_ts_type: kafka.timestamp_type",
    "iso: epoch_millis_to_iso8601(epoch_ms)", "micros: epoch_millis_to_micro(epoch_ms)", "meta.copied: name",
    "label: nothere",
]


def test_transforms_metadata_and_partitioned_commit(lake):
    """Adapted from the upstream delta_partitions and transform tests (tests/delta_partitions_tests.rs, src/transforms.rs)."""
    topic = lake.topic(partitions=2)
    table = lake.table(ROW_FIELDS, ["date"])
    lake.produce(topic, [
        (0, {"id": 1, "name": "alpha", "score": 1.5, "active": True, "ts": "2024-03-01T10:00:00Z", "tags": ["a", "b"], "meta": {"source": "web"}, "epoch_ms": 1709287200123, "label": "keep", "extra": "dropped"}, 1709287200500),
        (0, {"id": 2, "name": "beta", "score": 2, "active": False, "ts": "2024-03-01T12:30:00+02:00", "meta": {"source": "app"}, "epoch_ms": 5}, 1709287201000),
        (0, {"id": 3, "name": "gamma", "ts": "2024-03-02T00:00:00.123456Z", "tags": [], "epoch_ms": 1709337600999}, 1709287202000),
        (1, {"id": 4, "name": "delta", "ts": "2024-03-02T09:00:00Z", "score": -0.25, "tags": ["z", None], "epoch_ms": -1000}, 1709287203000),
        (1, {"id": 5, "name": "epsilon", "ts": "2024-03-01T23:59:59Z", "active": True, "score": 1e2, "epoch_ms": 1.9e12}, 1709287204000),
    ])
    args = lake.args(topic, table, *[a for t in TRANSFORMS for a in ("-t", t)], app="rows")
    lake.start(args)
    table.wait_version(1)
    quiet(table, 1, 4)

    actions = table.commit(1)
    assert set(kinds(actions)) - {"commitInfo"} == {"txn", "add"}
    assert txn_actions(actions) == {("rows-0", 2): 1, ("rows-1", 1): 1}
    table.check_adds(1)
    assert sorted(a["partitionValues"]["date"] for a in table.adds(1)) == ["2024-03-01", "2024-03-02"]

    common = {"kafka_topic": topic, "kafka_ts_type": 0}
    assert_rows_by(table.rows(), [
        {**common, "id": 1, "name": "alpha", "score": 1.5, "active": True, "ts": ts(2024, 3, 1, 10), "tags": ["a", "b"],
         "meta": {"source": "web", "copied": "alpha"}, "date": "2024-03-01", "kafka_offset": 0, "kafka_partition": 0,
         "kafka_ts": ts(2024, 3, 1, 10, 0, 0, us=500000), "iso": "2024-03-01T10:00:00.123Z", "micros": 1709287200123000, "label": "keep"},
        {**common, "id": 2, "name": "beta", "score": 2, "active": False, "ts": ts(2024, 3, 1, 10, 30), "tags": None,
         "meta": {"source": "app", "copied": "beta"}, "date": "2024-03-01", "kafka_offset": 1, "kafka_partition": 0,
         "kafka_ts": ts(2024, 3, 1, 10, 0, 1), "iso": "1970-01-01T00:00:00.005Z", "micros": 5000, "label": None},
        {**common, "id": 3, "name": "gamma", "score": None, "active": None, "ts": ts(2024, 3, 2, us=123456), "tags": [],
         "meta": {"source": None, "copied": "gamma"}, "date": "2024-03-02", "kafka_offset": 2, "kafka_partition": 0,
         "kafka_ts": ts(2024, 3, 1, 10, 0, 2), "iso": "2024-03-02T00:00:00.999Z", "micros": 1709337600999000, "label": None},
        {**common, "id": 4, "name": "delta", "score": -0.25, "active": None, "ts": ts(2024, 3, 2, 9), "tags": ["z", "null"],
         "meta": {"source": None, "copied": "delta"}, "date": "2024-03-02", "kafka_offset": 0, "kafka_partition": 1,
         "kafka_ts": ts(2024, 3, 1, 10, 0, 3), "iso": "1969-12-31T23:59:59Z", "micros": -1000000, "label": None},
        {**common, "id": 5, "name": "epsilon", "score": 100, "active": True, "ts": ts(2024, 3, 1, 23, 59, 59), "tags": None,
         "meta": {"source": None, "copied": "epsilon"}, "date": "2024-03-01", "kafka_offset": 1, "kafka_partition": 1,
         "kafka_ts": ts(2024, 3, 1, 10, 0, 4), "iso": "2030-03-17T17:46:40Z", "micros": 1900000000000000, "label": None},
    ], key="id")
    for add in table.adds(1):
        assert {r["date"] for r in table.file_rows(add)} == {add["partitionValues"]["date"]}


def test_general_jmespath_sources(lake):
    """Transform sources are full JMESPath: nested paths, builtins, indexes, or-expressions, filters, multiselects."""
    topic = lake.topic()
    table = lake.table([
        ("id", "integer"), ("name", "string"), ("tags", array("string")), ("meta", struct(("source", "string"), ("level", "long"))),
        ("src", "string"), ("n", "long"), ("joined", "string"), ("pair", struct(("a", "string"), ("b", "integer"))),
        ("pairtext", "string"), ("first", "string"), ("lvl", "long"), ("sel", array("string")),
    ])
    lake.produce(topic, [
        (0, {"id": 1, "name": "alpha", "tags": ["a", "b"], "meta": {"source": "web", "level": 2}}),
        (0, {"id": 2, "name": "beta", "tags": [], "meta": {"source": "app"}}),
        (0, {"id": 3, "name": "gamma", "tags": ["x"], "meta": {}}),
    ])
    transforms = ["src: meta.source", "n: length(tags)", "joined: join('-', tags)", "pair: {a: name, b: id}",
                  "pairtext: {a: name, b: id}", "first: tags[0]", "lvl: meta.level || `0`", "sel: tags[?@ != 'b']"]
    lake.start(lake.args(topic, table, *[a for t in transforms for a in ("-t", t)], app="jmes"))
    table.wait_version(1)
    quiet(table, 1, 4)
    assert table.txns() == {"jmes-0": 2}
    assert_rows_by(table.rows(), [
        {"id": 1, "name": "alpha", "tags": ["a", "b"], "meta": {"source": "web", "level": 2}, "src": "web", "n": 2,
         "joined": "a-b", "pair": {"a": "alpha", "b": 1}, "pairtext": JsonText('{"a":"alpha","b":1}'), "first": "a", "lvl": 2, "sel": ["a"]},
        {"id": 2, "name": "beta", "tags": [], "meta": {"source": "app", "level": None}, "src": "app", "n": 0,
         "joined": "", "pair": {"a": "beta", "b": 2}, "pairtext": JsonText('{"a":"beta","b":2}'), "first": None, "lvl": 0, "sel": []},
        {"id": 3, "name": "gamma", "tags": ["x"], "meta": {"source": None, "level": None}, "src": None, "n": 1,
         "joined": "x", "pair": {"a": "gamma", "b": 3}, "pairtext": JsonText('{"a":"gamma","b":3}'), "first": "x", "lvl": 0, "sel": ["x"]},
    ], key="id")


COERCION_FIELDS = [
    ("k", "integer"), ("s", "string"), ("t", "timestamp"), ("arr", array("string")), ("tarr", array("timestamp")),
    ("st", struct(("ss", "string"), ("tt", "timestamp"))), ("lst", array(struct(("x", "string")))),
]


def test_values_coerced_to_string_and_timestamp_columns(lake):
    """Adapted from src/coercions.rs: non-string JSON into string columns, RFC 3339 text into timestamps, at any depth."""
    topic = lake.topic()
    table = lake.table(COERCION_FIELDS)
    messages = [
        {"k": 1, "s": None}, {"k": 2, "s": 42}, {"k": 3, "s": 1.0}, {"k": 4, "s": False},
        {"k": 5, "s": {"b": 1, "a": [1, "x", None]}}, {"k": 6, "s": [1, 2]}, {"k": 7, "s": "text"},
        {"k": 8, "t": "2024-03-01T10:00:00.5-05:30"}, {"k": 9, "t": 1709287200000000},
        {"k": 10, "arr": [1, True, None, "x", {"a": 1}]}, {"k": 11, "tarr": ["2024-03-01T10:00:00Z", 1000000]},
        {"k": 12, "st": {"ss": 7, "tt": "2024-03-01T00:00:00Z"}}, {"k": 13, "lst": [{"x": 5}, {"x": None}, {"x": "y"}]},
        {"k": 14}, {"k": 15, "s": "1e5", "t": "2024-03-01T10:00:00+00:00"},
    ]
    lake.produce(topic, [(0, m) for m in messages])
    lake.start(lake.args(topic, table, app="coerce"))
    table.wait_version(1)
    quiet(table, 1, 4)
    assert table.txns() == {"coerce-0": 14}

    blank = {name: None for name, _ in COERCION_FIELDS}
    assert_rows_by(table.rows(), [
        {**blank, "k": 1, "s": JsonText("null")},
        {**blank, "k": 2, "s": JsonText("42")},
        {**blank, "k": 3, "s": JsonText("1.0")},
        {**blank, "k": 4, "s": JsonText("false")},
        {**blank, "k": 5, "s": JsonText('{"a":[1,"x",null],"b":1}')},
        {**blank, "k": 6, "s": JsonText("[1,2]")},
        {**blank, "k": 7, "s": "text"},
        {**blank, "k": 8, "t": ts(2024, 3, 1, 15, 30, 0, us=500000)},
        {**blank, "k": 9, "t": ts(2024, 3, 1, 10)},
        {**blank, "k": 10, "arr": [JsonText("1"), JsonText("true"), JsonText("null"), "x", JsonText('{"a":1}')]},
        {**blank, "k": 11, "tarr": [ts(2024, 3, 1, 10), ts(1970, 1, 1, 0, 0, 1)]},
        {**blank, "k": 12, "st": {"ss": JsonText("7"), "tt": ts(2024, 3, 1)}},
        {**blank, "k": 13, "lst": [{"x": JsonText("5")}, {"x": JsonText("null")}, {"x": "y"}]},
        {**blank, "k": 14},
        {**blank, "k": 15, "s": "1e5", "t": ts(2024, 3, 1, 10)},
    ], key="k")


def test_timestamp_and_date_inputs(lake):
    topic = lake.topic()
    table = lake.table([("k", "integer"), ("t", "timestamp"), ("d", "date")])
    lake.produce(topic, [(0, m) for m in [
        {"k": 1, "t": "2024-03-01 10:00:00"}, {"k": 2, "t": "2024-03-01"}, {"k": 3, "t": 1709287200},
        {"k": 4, "d": "2024-03-01"}, {"k": 5, "d": 19783}, {"k": 6, "t": None, "d": None},
    ]])
    lake.start(lake.args(topic, table, app="times"))
    table.wait_version(1)
    quiet(table, 1, 4)
    assert_rows_by(table.rows(), [
        {"k": 1, "t": ts(2024, 3, 1, 10), "d": None},
        {"k": 2, "t": ts(2024, 3, 1), "d": None},
        {"k": 3, "t": ts(1970, 1, 1, 0, 28, 29, us=287200), "d": None},
        {"k": 4, "t": None, "d": datetime.date(2024, 3, 1)},
        {"k": 5, "t": None, "d": datetime.date(2024, 3, 1)},
        {"k": 6, "t": None, "d": None},
    ], key="k")


def test_narrow_numeric_columns_and_epoch_functions(lake):
    topic = lake.topic()
    table = lake.table([("k", "integer"), ("sh", "short"), ("by", "byte"), ("fl", "float"), ("iso", "string"), ("isous", "string")])
    lake.produce(topic, [(0, m) for m in [
        {"k": 1, "sh": -300, "by": 7, "fl": 1.5, "ep": 1709287200, "us": 1709287200123456},
        {"k": 2, "sh": 32767, "by": -128, "fl": 0.1, "ep": -1, "us": 7},
        {"k": 3, "ep": 1.9e9, "us": 0},
    ]])
    lake.start(lake.args(topic, table, "-t", "iso: epoch_seconds_to_iso8601(ep)", "-t", "isous: epoch_micros_to_iso8601(us)", app="narrow"))
    table.wait_version(1)
    quiet(table, 1, 4)
    assert table.txns() == {"narrow-0": 2}
    assert_rows_by(table.rows(), [
        {"k": 1, "sh": -300, "by": 7, "fl": 1.5, "iso": "2024-03-01T10:00:00Z", "isous": "2024-03-01T10:00:00.123456Z"},
        # A float column holds single precision: 0.1 reads back as the nearest float32.
        {"k": 2, "sh": 32767, "by": -128, "fl": 0.10000000149011612, "iso": "1969-12-31T23:59:59Z", "isous": "1970-01-01T00:00:00.000007Z"},
        {"k": 3, "sh": None, "by": None, "fl": None, "iso": "2030-03-17T17:46:40Z", "isous": "1970-01-01T00:00:00Z"},
    ], key="k")


def test_partition_values_for_null_absent_and_typed_columns(lake):
    topic = lake.topic()
    table = lake.table([("id", "integer"), ("region", "string"), ("bucket", "integer")], ["region", "bucket"])
    lake.produce(topic, [(0, m) for m in [
        {"id": 1, "region": "eu", "bucket": 1}, {"id": 2, "region": None, "bucket": 2}, {"id": 3, "bucket": 3},
        {"id": 4, "region": "us", "bucket": None}, {"id": 5, "region": "a b/c=d%", "bucket": 1},
        {"id": 6, "region": "eu", "bucket": 1}, {"id": 8, "region": 7, "bucket": 5},
    ]])
    lake.start(lake.args(topic, table, app="parts"))
    table.wait_version(1)
    quiet(table, 1, 4)
    table.check_adds(1)
    recorded = Counter(tuple(sorted(a["partitionValues"].items())) for a in table.adds(1))
    assert recorded == Counter(tuple(sorted(pv.items())) for pv in [
        {"region": "eu", "bucket": "1"}, {"region": "null", "bucket": "2"}, {"region": None, "bucket": "3"},
        {"region": "us", "bucket": None}, {"region": "a b/c=d%", "bucket": "1"}, {"region": "7", "bucket": "5"},
    ])
    assert_rows_by(table.rows(), [
        {"id": 1, "region": "eu", "bucket": 1}, {"id": 2, "region": "null", "bucket": 2}, {"id": 3, "region": None, "bucket": 3},
        {"id": 4, "region": "us", "bucket": None}, {"id": 5, "region": "a b/c=d%", "bucket": 1},
        {"id": 6, "region": "eu", "bucket": 1}, {"id": 8, "region": "7", "bucket": 5},
    ], key="id")
    assert table.txns() == {"parts-0": 6}


@pytest.mark.parametrize("kind, value", [("integer", "abc"), ("boolean", "true"), ("integer", 3000000000), ("short", 40000)])
def test_value_the_schema_cannot_hold_stops_the_run(lake, kind, value):
    topic = lake.topic()
    table = lake.table([("n", "string"), ("v", kind)])
    dlq = lake.table([("base64_bytes", "string"), ("json_string", "string"), ("error", "string"), ("timestamp", "timestamp")])
    lake.produce(topic, [(0, {"n": "before"}), (0, {"n": "bad", "v": value}), (0, {"n": "after"})])
    run = lake.start(lake.args(topic, table, "--dlq_table_location", dlq.uri, app="halt"))
    assert run.wait(30) == 101, run.output()
    assert table.versions() == [0]
    assert dlq.versions() == [0]
    assert lake.lock_items(table) == [] and lake.lock_items(dlq) == []
