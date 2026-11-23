"""The Postgres sink: append and upsert batches, column/type mapping, and refused batches.

Adapted from upstream dev/config/examples/kafka.postgres.sink.yml,
dev/postgres/migrations/users.sql and internal/sinks/postgres_test.go
(merge SQL, last row per key, SQLSTATE classification, startup probe).
"""

from decimal import Decimal
from urllib.parse import urlsplit

import pytest

from streams import canon, dlq_block, pipeline, postgres_sink


def error_line(stderr):
    return next((line for line in stderr.splitlines() if line.startswith("Error: ")), "")


def assert_refused(result, code, exit_status=10):
    assert result.returncode == exit_status, result.stderr
    assert error_line(result.stderr).startswith(f"Error: [{code}] "), result.stderr


EVENTS = [
    '{"ts":"2026-01-01T00:00:00Z","user_id":7,"action":"click","n":1,"score":1.5,"ok":true,"meta":{"a":1,"b":"x"},"tags":["t1","t2"]}',
    '{"ts":"2026-01-01T00:00:01Z","user_id":8,"action":"skip","n":2,"score":2,"ok":false,"meta":{"a":2},"tags":[]}',
    '{"ts":"2026-01-01T01:00:02+01:00","user_id":9,"action":"view","n":3,"score":3,"ok":null,"meta":{"b":"y"},"tags":["t3"]}',
]
EVENTS_SQL = """
SELECT CAST(ts AS TIMESTAMPTZ) AS ts, CAST(user_id AS TEXT) AS user_id, action, n, score, ok, meta, tags,
       CAST(ts AS DATE) AS d, CAST(n AS DECIMAL(10,2)) / 4 AS amount
FROM batch WHERE action <> 'skip'
"""
EVENTS_TABLE = ("ts timestamptz, user_id text, action text, n bigint, score double precision, ok boolean, meta jsonb, "
                "tags jsonb, d date, amount numeric(10,2), note text DEFAULT 'dflt'")
EVENTS_QUERY = ("SELECT to_char(ts AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'), user_id, action, n, score, ok, meta, tags, "
                "d::text, amount, note FROM \"{}\" ORDER BY n")


def test_append_maps_types_and_keeps_existing_rows(stream):
    source = stream.topic("in")
    table = stream.table("events", EVENTS_TABLE)
    stream.sql(f'INSERT INTO "{table}" (user_id, action, n) VALUES (%s, %s, %s)', ("sentinel", "keep", 0))
    stream.kafka.produce(source, EVENTS)
    config = stream.config(pipeline(EVENTS_SQL, postgres_sink(table, "append"), batch_size=2))
    result = stream.run(["run", config, "--max-msgs", "3", "--stats-json", "stats.json"], env={"SQLFLOW_INPUT_TOPIC": source})
    assert result.returncode == 0, result.stderr
    rows = stream.sql(EVENTS_QUERY.format(table))
    # jsonb compares as JSON with booleans distinct; ok is a Postgres boolean, not a number.
    assert [row[:6] + row[8:] for row in rows] == [
        (None, "sentinel", "keep", 0, None, None, None, None, "dflt"),
        ("2026-01-01 00:00:00", "7", "click", 1, 1.5, True, "2026-01-01", Decimal("0.25"), "dflt"),
        ("2026-01-01 00:00:02", "9", "view", 3, 3.0, None, "2026-01-01", Decimal("0.75"), "dflt"),
    ]
    assert [type(row[5]) for row in rows] == [type(None), bool, type(None)]
    assert [(canon(row[6]), canon(row[7])) for row in rows] == [
        (canon(None), canon(None)),
        (canon({"a": 1, "b": "x"}), canon(["t1", "t2"])),
        (canon({"b": "y"}), canon(["t3"])),
    ]
    assert stream.kafka.committed(stream.group, source) == [3]


def test_upsert_keeps_the_last_row_per_key(stream):
    source = stream.topic("in")
    table = stream.table("kv", "k text PRIMARY KEY, v bigint, note text DEFAULT 'new'")
    stream.sql(f"INSERT INTO \"{table}\" VALUES ('a', 0, 'old'), ('z', 99, 'sentinel')")
    stream.kafka.produce(source, ['{"k":"a","v":1}', '{"k":"b","v":2}', '{"k":"a","v":3}', '{"k":"b","v":5}', '{"k":"c","v":6}'])
    config = stream.config(pipeline("SELECT k, v FROM batch", postgres_sink(table, "upsert", ["k"]), batch_size=3))
    result = stream.run(["run", config, "--max-msgs", "5"], env={"SQLFLOW_INPUT_TOPIC": source})
    assert result.returncode == 0, result.stderr
    assert stream.sql(f'SELECT k, v, note FROM "{table}" ORDER BY k') == [
        ("a", 3, "old"), ("b", 5, "new"), ("c", 6, "new"), ("z", 99, "sentinel")]
    assert stream.kafka.committed(stream.group, source) == [5]


def test_upsert_of_key_columns_only_inserts_missing_keys(stream):
    source = stream.topic("in")
    table = stream.table("kv", "k text PRIMARY KEY, v bigint DEFAULT -1")
    stream.sql(f"INSERT INTO \"{table}\" VALUES ('a', 10)")
    stream.kafka.produce(source, ['{"k":"a","v":1}', '{"k":"b","v":2}', '{"k":"b","v":3}'])
    config = stream.config(pipeline("SELECT k FROM batch", postgres_sink(table, "upsert", ["k"])))
    result = stream.run(["run", config, "--max-msgs", "3"], env={"SQLFLOW_INPUT_TOPIC": source})
    assert result.returncode == 0, result.stderr
    assert stream.sql(f'SELECT k, v FROM "{table}" ORDER BY k') == [("a", 10), ("b", -1)]


def test_upsert_with_a_composite_key(stream):
    source = stream.topic("in")
    table = stream.table("pairs", "region text NOT NULL, day date NOT NULL, total bigint, PRIMARY KEY (region, day)")
    stream.sql(f"INSERT INTO \"{table}\" VALUES ('eu', '2026-01-01', 100), ('us', '2026-01-02', 7)")
    stream.kafka.produce(source, [
        '{"region":"eu","day":"2026-01-01","amount":1}', '{"region":"eu","day":"2026-01-02","amount":2}',
        '{"region":"us","day":"2026-01-01","amount":3}', '{"region":"eu","day":"2026-01-01","amount":4}',
    ])
    sql = "SELECT region, CAST(day AS DATE) AS day, sum(amount)::BIGINT AS total FROM batch GROUP BY ALL"
    config = stream.config(pipeline(sql, postgres_sink(table, "upsert", ["region", "day"]), batch_size=2))
    result = stream.run(["run", config, "--max-msgs", "4"], env={"SQLFLOW_INPUT_TOPIC": source})
    assert result.returncode == 0, result.stderr
    assert stream.sql(f'SELECT region, day::text, total FROM "{table}" ORDER BY region, day') == [
        ("eu", "2026-01-01", 4), ("eu", "2026-01-02", 2), ("us", "2026-01-01", 3), ("us", "2026-01-02", 7)]


@pytest.mark.parametrize("columns", ["k text NOT NULL, v bigint", "k text, v bigint, UNIQUE (k)"])
def test_upsert_target_is_checked_at_startup(stream, columns):
    source = stream.topic("in")
    table = stream.table("t", columns)
    stream.kafka.produce(source, ['{"k":"a","v":1}'])
    config = stream.config(pipeline("SELECT k, v FROM batch", postgres_sink(table, "upsert", ["k"])))
    result = stream.run(["run", config, "--max-msgs", "1"], env={"SQLFLOW_INPUT_TOPIC": source})
    assert_refused(result, "user.sink.invalid")
    # No unique index covers exactly the key, or the key column is nullable.
    assert table in error_line(result.stderr)
    assert stream.sql(f'SELECT count(*) FROM "{table}"') == [(0,)]
    assert stream.kafka.committed(stream.group, source) == [None]


def test_missing_table_is_refused_at_startup(stream):
    source = stream.topic("in")
    stream.kafka.produce(source, ['{"k":"a","v":1}'])
    missing = stream.prefix + "_absent"
    config = stream.config(pipeline("SELECT k, v FROM batch", postgres_sink(missing, "append")))
    result = stream.run(["run", config, "--max-msgs", "1"], env={"SQLFLOW_INPUT_TOPIC": source})
    assert_refused(result, "user.sink.invalid")
    assert missing in error_line(result.stderr)
    assert stream.sql("SELECT count(*) FROM pg_tables WHERE tablename = %s", (missing,)) == [(0,)]
    assert stream.kafka.committed(stream.group, source) == [None]


@pytest.mark.parametrize(("columns", "sql", "values", "code"), [
    # A result column the table lacks.
    ("k text, v bigint", "SELECT k, v, v * 10 AS w FROM batch", ['{"k":"a","v":1}', '{"k":"b","v":2}'], "user.sink.invalid"),
    # A BIGINT result for a text column, and a text result for an integer column.
    ("k text, v bigint", "SELECT v AS k, v FROM batch", ['{"k":"a","v":1}', '{"k":"b","v":2}'], "user.sink.encode_failed"),
    ("k bigint, v bigint", "SELECT k, v FROM batch", ['{"k":"a","v":1}', '{"k":"b","v":2}'], "user.sink.encode_failed"),
    # A value too wide for its column.
    ("k text, v smallint", "SELECT k, v FROM batch", ['{"k":"a","v":1}', '{"k":"b","v":70000}'], "user.sink.encode_failed"),
    # A NULL for a NOT NULL column.
    ("k text NOT NULL, v bigint", "SELECT k, v FROM batch", ['{"k":"a","v":1}', '{"k":null,"v":2}'], "user.sink.invalid"),
])
def test_refused_batch_writes_nothing_and_commits_nothing(stream, columns, sql, values, code):
    source = stream.topic("in")
    table = stream.table("t", columns)
    stream.kafka.produce(source, values)
    config = stream.config(pipeline(sql, postgres_sink(table, "append"), batch_size=5))
    result = stream.run(["run", config, "--max-msgs", "2", "--stats-json", "stats.json"], env={"SQLFLOW_INPUT_TOPIC": source})
    assert_refused(result, code)
    assert stream.sql(f'SELECT count(*) FROM "{table}"') == [(0,)]
    assert stream.kafka.committed(stream.group, source) == [None]
    assert not (stream.root / "stats.json").exists()


def test_failed_batch_rolls_back_whole_after_earlier_batches(stream):
    source = stream.topic("in")
    table = stream.table("uniq", "k text PRIMARY KEY, v bigint")
    stream.kafka.produce(source, ['{"k":"a","v":1}', '{"k":"b","v":2}', '{"k":"c","v":3}', '{"k":"a","v":4}', '{"k":"d","v":5}'])
    config = stream.config(pipeline("SELECT k, v FROM batch", postgres_sink(table, "append"), batch_size=2))
    result = stream.run(["run", config, "--max-msgs", "5"], env={"SQLFLOW_INPUT_TOPIC": source})
    assert_refused(result, "user.sink.invalid")
    # The first batch landed and was committed; the second (c, a) is absent as a whole.
    assert stream.sql(f'SELECT k, v FROM "{table}" ORDER BY k') == [("a", 1), ("b", 2)]
    assert stream.kafka.committed(stream.group, source) == [2]


def test_sink_failures_are_not_dead_lettered(stream):
    source, dlq = stream.topic("in"), stream.topic("dlq")
    table = stream.table("narrow", "k text")
    stream.kafka.produce(source, ['{"k":"a"}', '{"k":"b"}'])
    text = pipeline("SELECT k, 1 AS extra FROM batch", postgres_sink(table, "append"), batch_size=5, extra=dlq_block("DLQ"))
    result = stream.run(["run", stream.config(text), "--max-msgs", "2"], env={"SQLFLOW_INPUT_TOPIC": source, "SQLFLOW_DLQ_TOPIC": dlq})
    assert_refused(result, "user.sink.invalid")
    assert stream.kafka.read(dlq) == []
    assert stream.sql(f'SELECT count(*) FROM "{table}"') == [(0,)]
    assert stream.kafka.committed(stream.group, source) == [None]


def test_unreachable_and_unauthorized_postgres(stream):
    source = stream.topic("in")
    table = stream.table("t", "k text")
    stream.kafka.produce(source, ['{"k":"a"}'])
    env = {"SQLFLOW_INPUT_TOPIC": source}
    sink = postgres_sink(table, "append").replace('"{{ SQLFLOW_PG_DSN }}"', "postgresql://app:app-pass-1@127.0.0.1:1/sink?sslmode=disable")
    result = stream.run(["run", stream.config(pipeline("SELECT k FROM batch", sink), "down.yml"), "--max-msgs", "1"], env=env)
    assert_refused(result, "system.sink.unreachable", exit_status=12)

    wrong = postgres_sink(table, "append").replace('"{{ SQLFLOW_PG_DSN }}"', '"' + stream.venue.dsn.replace("app-pass-1", "wrong") + '"')
    result = stream.run(["run", stream.config(pipeline("SELECT k FROM batch", wrong), "auth.yml"), "--max-msgs", "1"], env=env)
    assert_refused(result, "user.sink.invalid")
    assert stream.sql(f'SELECT count(*) FROM "{table}"') == [(0,)]
    assert stream.kafka.committed(stream.group, source) == [None]


def test_key_value_dsn_and_schema_qualified_table(stream):
    source = stream.topic("in")
    table = stream.table("qualified", "k text PRIMARY KEY, v bigint")
    stream.kafka.produce(source, ['{"k":"a","v":1}', '{"k":"a","v":2}', '{"k":"b","v":3}'])
    url = urlsplit(stream.venue.dsn)
    dsn = f"host={url.hostname} port={url.port} user={url.username} password={url.password} dbname={url.path.lstrip('/')} sslmode=disable"
    sink = postgres_sink(f"public.{table}", "upsert", ["k"]).replace('"{{ SQLFLOW_PG_DSN }}"', f'"{dsn}"')
    result = stream.run(["run", stream.config(pipeline("SELECT k, v FROM batch", sink)), "--max-msgs", "3"],
                        env={"SQLFLOW_INPUT_TOPIC": source})
    assert result.returncode == 0, result.stderr
    assert stream.sql(f'SELECT k, v FROM "{table}" ORDER BY k') == [("a", 2), ("b", 3)]
    assert stream.kafka.committed(stream.group, source) == [3]
