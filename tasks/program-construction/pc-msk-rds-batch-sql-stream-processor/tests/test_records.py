"""Single runs into Kafka and console sinks: batches, result rows as records, keys and commits.

Scenarios adapted from upstream tests/release/test_image.py
(test_handler_inferred_mem_aggregates_every_message,
test_handler_inferred_mem_preserves_arrays_and_unioned_fields) and
internal/handlers/{inferred,json_null}_test.go, internal/sinks/kafka_test.go.
"""

import json
from collections import Counter

from streams import assert_records, canon, decode, kafka_sink, pipeline, same, source_block

TYPES_SQL = """
SELECT id, name, score, ok, nothing, tags, nested, kafka_topic, kafka_partition, kafka_offset,
       CAST('2026-01-02T03:04:05.123Z' AS TIMESTAMPTZ) AS ts, DATE '2026-01-02' AS d,
       CAST(1.50 AS DECIMAL(10,2)) AS dec, sum(id) OVER () AS total, 1.0::DOUBLE AS one_f, 3::DOUBLE / 2 AS half,
       typeof(score) AS score_type, typeof(nested) AS nested_type, typeof(id) AS id_type
FROM batch
"""
TYPES_INPUT = [
    '{"id":1,"name":"a","score":1,"ok":true,"nothing":null,"tags":["x","y"],"nested":{"k":1},"ignored_first":0}',
    '{"id":2,"name":"b","score":2.5,"ok":false,"nothing":null,"tags":[],"nested":{"j":"s"},"extra":9}',
    '{"id":3,"name":"c<&>","score":3,"ok":null,"nothing":null,"tags":["z"],"nested":{"k":2}}',
    '{"id":4,"name":"d","score":4,"ok":true,"nothing":null,"tags":["q"],"nested":{"k":3}}',
]


def test_result_rows_become_json_records(stream):
    source, output = stream.topic("in"), stream.topic("out")
    stream.kafka.produce(source, TYPES_INPUT)
    config = stream.config(pipeline(TYPES_SQL, kafka_sink(key="name"), batch_size=3))
    result = stream.run(["run", config, "--max-msgs", "4", "--stats-json", "stats.json"],
                        env={"SQLFLOW_INPUT_TOPIC": source, "SQLFLOW_OUTPUT_TOPIC": output})
    assert result.returncode == 0, result.stderr
    common = {"ts": "2026-01-02T03:04:05.123Z", "d": "2026-01-02", "dec": "1.5", "one_f": 1, "half": 1.5,
              "id_type": "BIGINT", "nothing": None, "kafka_topic": source, "kafka_partition": 0}
    first = {**common, "total": "6", "score_type": "DOUBLE", "nested_type": "STRUCT(k BIGINT, j VARCHAR)"}
    expected = [
        {**first, "id": 1, "name": "a", "score": 1, "ok": True, "tags": ["x", "y"], "nested": {"k": 1, "j": None}, "kafka_offset": 0},
        {**first, "id": 2, "name": "b", "score": 2.5, "ok": False, "tags": [], "nested": {"k": None, "j": "s"}, "kafka_offset": 1},
        {**first, "id": 3, "name": "c<&>", "score": 3, "ok": None, "tags": ["z"], "nested": {"k": 2, "j": None}, "kafka_offset": 2},
        {**common, "total": "4", "score_type": "BIGINT", "nested_type": "STRUCT(k BIGINT)",
         "id": 4, "name": "d", "score": 4, "ok": True, "tags": ["q"], "nested": {"k": 3}, "kafka_offset": 3},
    ]
    records = stream.kafka.read(output)
    assert_records([decode(r["value"]) for r in records], expected)
    # Each record is keyed by the text of its row's `name`.
    assert sorted((r["key"], decode(r["value"])["name"]) for r in records) == [
        (b"a", "a"), (b"b", "b"), (b"c<&>", "c<&>"), (b"d", "d")]
    assert same(stream.stats(), {"messages_consumed": 4, "num_errors": 0})
    assert stream.kafka.committed(stream.group, source) == [4]


def test_columns_come_from_the_first_message_of_each_batch(stream):
    source, output = stream.topic("in"), stream.topic("out")
    stream.kafka.produce(source, [
        '{"a":1,"b":"x"}', '{"a":2,"c":true}', '{"b":"y","a":2.5}',
        '{"c":false,"a":7}', '{"a":8,"b":"z","c":true}',
    ])
    sql = "SELECT * EXCLUDE (kafka_topic), typeof(a) AS a_type FROM batch"
    config = stream.config(pipeline(sql, kafka_sink(), batch_size=3))
    result = stream.run(["run", config, "--max-msgs", "5"], env={"SQLFLOW_INPUT_TOPIC": source, "SQLFLOW_OUTPUT_TOPIC": output})
    assert result.returncode == 0, result.stderr
    assert_records(stream.kafka.values(output), [
        {"a": 1, "b": "x", "a_type": "DOUBLE", "kafka_partition": 0, "kafka_offset": 0},
        {"a": 2, "b": None, "a_type": "DOUBLE", "kafka_partition": 0, "kafka_offset": 1},
        {"a": 2.5, "b": "y", "a_type": "DOUBLE", "kafka_partition": 0, "kafka_offset": 2},
        {"a": 7, "c": False, "a_type": "BIGINT", "kafka_partition": 0, "kafka_offset": 3},
        {"a": 8, "c": True, "a_type": "BIGINT", "kafka_partition": 0, "kafka_offset": 4},
    ])


def test_batches_and_final_partial_batch(stream):
    source, output = stream.topic("in"), stream.topic("out")
    stream.kafka.produce(source, [{"n": i} for i in range(1, 7)])
    sql = "SELECT n, count(*) OVER () AS batch_rows, min(kafka_offset) OVER () AS first_offset FROM batch"
    config = stream.config(pipeline(sql, kafka_sink(), batch_size=2))
    result = stream.run(["run", config, "--max-msgs", "5", "--stats-json", "stats.json"],
                        env={"SQLFLOW_INPUT_TOPIC": source, "SQLFLOW_OUTPUT_TOPIC": output})
    assert result.returncode == 0, result.stderr
    assert_records(stream.kafka.values(output), [
        {"n": 1, "batch_rows": 2, "first_offset": 0}, {"n": 2, "batch_rows": 2, "first_offset": 0},
        {"n": 3, "batch_rows": 2, "first_offset": 2}, {"n": 4, "batch_rows": 2, "first_offset": 2},
        {"n": 5, "batch_rows": 1, "first_offset": 4},
    ])
    assert same(stream.stats(), {"messages_consumed": 5, "num_errors": 0})
    assert stream.kafka.committed(stream.group, source) == [5]


def test_aggregate_rows_per_batch(stream):
    source, output = stream.topic("in"), stream.topic("out")
    cities = ["a", "b", "a", "a", "c", "a", "b"]
    stream.kafka.produce(source, [{"properties": {"city": c}, "user": {"id": str(i)}} for i, c in enumerate(cities)])
    sql = "SELECT properties.city AS city, count(*) AS n, list(user.id ORDER BY user.id) AS users FROM batch GROUP BY city"
    config = stream.config(pipeline(sql, kafka_sink(), batch_size=3))
    result = stream.run(["run", config, "--max-msgs", "7"], env={"SQLFLOW_INPUT_TOPIC": source, "SQLFLOW_OUTPUT_TOPIC": output})
    assert result.returncode == 0, result.stderr
    assert_records(stream.kafka.values(output), [
        {"city": "a", "n": 2, "users": ["0", "2"]}, {"city": "b", "n": 1, "users": ["1"]},
        {"city": "a", "n": 2, "users": ["3", "5"]}, {"city": "c", "n": 1, "users": ["4"]},
        {"city": "b", "n": 1, "users": ["6"]},
    ])
    assert stream.kafka.committed(stream.group, source) == [7]


def test_text_escapes_and_wide_numbers(stream):
    source, output = stream.topic("in"), stream.topic("out")
    stream.kafka.produce(source, ['{"s":"line one\\nline two caf\\u00e9","big":9007199254740993,"neg":-5,"f":1e3,"z":0.0}'])
    sql = "SELECT s, big, neg, f, z, typeof(f) AS ft, length(s) AS len FROM batch"
    config = stream.config(pipeline(sql, kafka_sink(), batch_size=1))
    result = stream.run(["run", config, "--max-msgs", "1"], env={"SQLFLOW_INPUT_TOPIC": source, "SQLFLOW_OUTPUT_TOPIC": output})
    assert result.returncode == 0, result.stderr
    [record] = stream.kafka.values(output)
    assert same(record, {"s": "line one\nline two café", "big": 9007199254740993, "neg": -5, "f": 1000, "z": 0,
                         "ft": "DOUBLE", "len": 22})


def test_console_sink_writes_rows_to_stdout(stream):
    source = stream.topic("in")
    stream.kafka.produce(source, ['{"id":1,"name":"a"}', '{"id":2,"name":"b"}', '{"id":3,"name":"c"}'])
    config = stream.config(pipeline("SELECT id, name, id * 10 AS tens FROM batch", "  sink:\n    type: console\n", batch_size=2))
    result = stream.run(["run", "-c", config, "--max-msgs", "3"], env={"SQLFLOW_INPUT_TOPIC": source})
    assert result.returncode == 0, result.stderr
    rows = [decode(line) for line in result.stdout.splitlines()]
    assert_records(rows, [{"id": 1, "name": "a", "tens": 10}, {"id": 2, "name": "b", "tens": 20}, {"id": 3, "name": "c", "tens": 30}])
    assert stream.kafka.committed(stream.group, source) == [3]


def keyed_records(records):
    return Counter((r["key"], canon(decode(r["value"]))) for r in records)


def test_integer_key_and_unkeyed_records(stream):
    source, keyed, plain = stream.topic("in"), stream.topic("keyed"), stream.topic("plain")
    stream.kafka.produce(source, ['{"id":7,"name":"a"}', '{"id":8,"name":null}'])
    env = {"SQLFLOW_INPUT_TOPIC": source}
    config = stream.config(pipeline("SELECT id, name FROM batch", kafka_sink(keyed, key="id"), batch_size=5), "keyed.yml")
    assert stream.run(["run", config, "--max-msgs", "2"], env=env).returncode == 0
    assert keyed_records(stream.kafka.read(keyed)) == Counter([(b"7", canon({"id": 7, "name": "a"})), (b"8", canon({"id": 8, "name": None}))])

    stream.group += "-plain"
    config = stream.config(pipeline("SELECT id FROM batch", kafka_sink(plain), batch_size=5), "plain.yml")
    assert stream.run(["run", config, "--max-msgs", "2"], env=env).returncode == 0
    assert keyed_records(stream.kafka.read(plain)) == Counter([(None, canon({"id": 7})), (None, canon({"id": 8}))])


def error_line(stderr):
    return next((line for line in stderr.splitlines() if line.startswith("Error: ")), "")


def test_null_or_missing_key_column_fails_the_batch(stream):
    source, output = stream.topic("in"), stream.topic("out")
    stream.kafka.produce(source, ['{"id":7,"name":"a"}', '{"id":8,"name":null}'])
    env = {"SQLFLOW_INPUT_TOPIC": source, "SQLFLOW_OUTPUT_TOPIC": output}
    for sql in ("SELECT id, name FROM batch", "SELECT id FROM batch"):
        config = stream.config(pipeline(sql, kafka_sink(key="name"), batch_size=5))
        result = stream.run(["run", config, "--max-msgs", "2"], env=env)
        assert result.returncode == 10, result.stderr
        assert error_line(result.stderr).startswith("Error: [user.sink.encode_failed] "), result.stderr
        assert stream.kafka.read(output) == []
        assert stream.kafka.committed(stream.group, source) == [None]


def test_metadata_across_topics_and_partitions(stream):
    first, second, output = stream.topic("a", partitions=2), stream.topic("b"), stream.topic("out")
    stream.kafka.produce(first, [{"n": 1}, {"n": 2}], partition=0)
    stream.kafka.produce(first, [{"n": 3}], partition=1)
    stream.kafka.produce(second, [{"n": 4}])
    sql = "SELECT n, kafka_topic, kafka_partition, kafka_offset, count(*) OVER () AS batch_rows FROM batch"
    config = stream.config(pipeline(sql, kafka_sink(), batch_size=10, source=source_block(f"{first}, {second}")))
    result = stream.run(["run", config, "--max-msgs", "4", "--stats-json", "stats.json"], env={"SQLFLOW_OUTPUT_TOPIC": output})
    assert result.returncode == 0, result.stderr
    assert_records(stream.kafka.values(output), [
        {"n": 1, "kafka_topic": first, "kafka_partition": 0, "kafka_offset": 0, "batch_rows": 4},
        {"n": 2, "kafka_topic": first, "kafka_partition": 0, "kafka_offset": 1, "batch_rows": 4},
        {"n": 3, "kafka_topic": first, "kafka_partition": 1, "kafka_offset": 0, "batch_rows": 4},
        {"n": 4, "kafka_topic": second, "kafka_partition": 0, "kafka_offset": 0, "batch_rows": 4},
    ])
    assert stream.kafka.committed(stream.group, first, partitions=2) == [2, 1]
    assert stream.kafka.committed(stream.group, second) == [1]
    assert same(stream.stats(), {"messages_consumed": 4, "num_errors": 0})


def test_batches_with_no_result_rows_still_commit(stream):
    source, output = stream.topic("in"), stream.topic("out")
    stream.kafka.produce(source, [{"n": 1}, {"n": 2}, {"n": 3}])
    config = stream.config(pipeline("SELECT n FROM batch WHERE n > 100", kafka_sink(), batch_size=2))
    result = stream.run(["run", config, "--max-msgs", "3"], env={"SQLFLOW_INPUT_TOPIC": source, "SQLFLOW_OUTPUT_TOPIC": output})
    assert result.returncode == 0, result.stderr
    assert stream.kafka.read(output) == []
    assert stream.kafka.committed(stream.group, source) == [3]


def test_disk_batch_infers_from_every_message(stream):
    source, output = stream.topic("in"), stream.topic("out")
    stream.kafka.produce(source, ['{"k":"a","v":1}', '{"k":"b","v":2.5,"x":true}', '{"k":"c","v":3}'])
    cache = stream.root / "cache"
    sql = "SELECT k, v, x, typeof(v) AS t FROM batch"
    text = pipeline(sql, kafka_sink(), batch_size=3, handler="handlers.InferredDiskBatch")
    text = text.replace("    sql: |\n", f"    sql_results_cache_dir: {cache}\n    sql: |\n")
    config = stream.config(text)
    result = stream.run(["run", config, "--max-msgs", "3"], env={"SQLFLOW_INPUT_TOPIC": source, "SQLFLOW_OUTPUT_TOPIC": output})
    assert result.returncode == 0, result.stderr
    # Unlike the in-memory handler, a key only a later message carries is still a column.
    assert_records(stream.kafka.values(output), [
        {"k": "a", "v": 1, "x": None, "t": "DOUBLE"}, {"k": "b", "v": 2.5, "x": True, "t": "DOUBLE"},
        {"k": "c", "v": 3, "x": None, "t": "DOUBLE"},
    ])
    assert stream.kafka.committed(stream.group, source) == [3]


def test_disk_batch_has_no_metadata_columns(stream):
    source, output = stream.topic("in"), stream.topic("out")
    stream.kafka.produce(source, ['{"k":"a"}'])
    config = stream.config(pipeline("SELECT k, kafka_offset FROM batch", kafka_sink(), batch_size=1,
                                    handler="handlers.InferredDiskBatch"))
    result = stream.run(["run", config, "--max-msgs", "1"],
                        env={"SQLFLOW_INPUT_TOPIC": source, "SQLFLOW_OUTPUT_TOPIC": output,
                             "SQLFLOW_SQL_RESULTS_CACHE_DIR": str(stream.root / "cache")})
    assert result.returncode == 1, result.stderr
    assert stream.kafka.read(output) == []
    assert stream.kafka.committed(stream.group, source) == [None]


def test_unreachable_kafka_sink_at_startup(stream):
    source = stream.topic("in")
    stream.kafka.produce(source, ['{"k":"a"}'])
    sink = "  sink:\n    type: kafka\n    kafka:\n      brokers: [127.0.0.1:1]\n      topic: nowhere\n"
    result = stream.run(["run", stream.config(pipeline("SELECT k FROM batch", sink)), "--max-msgs", "1"],
                        env={"SQLFLOW_INPUT_TOPIC": source})
    assert result.returncode == 12, result.stderr
    assert error_line(result.stderr).startswith("Error: [system.sink.unreachable] ")
    assert stream.kafka.committed(stream.group, source) == [None]


def test_stats_file_is_one_json_object(stream):
    source = stream.topic("in")
    stream.kafka.produce(source, [{"n": 1}, {"n": 2}])
    config = stream.config(pipeline("SELECT n FROM batch", "  sink:\n    type: noop\n", batch_size=1))
    result = stream.run(["run", config, "--max-msgs", "2", "--stats-json", "stats.json"], env={"SQLFLOW_INPUT_TOPIC": source})
    assert result.returncode == 0, result.stderr
    stats = json.loads((stream.root / "stats.json").read_text())
    assert set(stats) == {"messages_consumed", "num_errors"}
    assert all(type(v) in (int, float) for v in stats.values())
    assert same(stats, {"messages_consumed": 2, "num_errors": 0})


def test_template_defaults_and_overrides_at_run(stream):
    source = stream.topic("in")
    stream.kafka.produce(source, [{"n": i} for i in range(4)])
    text = pipeline("SELECT n, count(*) OVER () AS batch_rows FROM batch", kafka_sink("{{ SQLFLOW_OUT|default('x') }}"),
                    batch_size=10).replace("  batch_size: 10\n", "  batch_size: {{ SQLFLOW_BATCH|default(3) }}\n")
    config = stream.config(text)
    for name, env, sizes in [("dflt", {}, [3, 3, 3, 1]), ("set", {"SQLFLOW_BATCH": "4"}, [4, 4, 4, 4])]:
        output = stream.topic(name)
        stream.group = f"{stream.prefix}-{name}"
        result = stream.run(["run", config, "--max-msgs", "4"], env={"SQLFLOW_INPUT_TOPIC": source, "SQLFLOW_OUT": output, **env})
        assert result.returncode == 0, result.stderr
        assert_records(stream.kafka.values(output), [{"n": i, "batch_rows": b} for i, b in enumerate(sizes)])


def test_run_refuses_an_invalid_config_before_consuming(stream):
    source, output = stream.topic("in"), stream.topic("out")
    stream.kafka.produce(source, [{"n": 1}])
    text = pipeline("SELECT n FROM batch", kafka_sink()).replace("  batch_size: 10\n", "  batch_size: 10\n  bogus_key: 1\n")
    result = stream.run(["run", stream.config(text), "--max-msgs", "1"], env={"SQLFLOW_INPUT_TOPIC": source, "SQLFLOW_OUTPUT_TOPIC": output})
    assert result.returncode == 10, result.stderr
    # run parses strictly but reports the refusal as a parse failure, unlike validate.
    assert error_line(result.stderr).startswith("Error: [user.config.parse_failed] ") and "bogus_key" in result.stderr
    assert stream.kafka.read(output) == []
    assert stream.kafka.committed(stream.group, source) == [None]


def test_startup_tables_are_visible_to_every_batch(stream):
    source, output = stream.topic("in"), stream.topic("out")
    stream.kafka.produce(source, ['{"code":"a","n":1}', '{"code":"b","n":2}', '{"code":"z","n":3}'])
    tables = ("tables:\n  sql:\n    - name: cities\n"
              "      sql: CREATE TABLE cities AS SELECT * FROM (VALUES ('a', 'Austin'), ('b', 'Boston')) t(code, name)\n")
    sql = "SELECT b.n, c.name FROM batch b LEFT JOIN cities c ON c.code = b.code"
    config = stream.config(pipeline(sql, kafka_sink(), batch_size=2, tables=tables))
    result = stream.run(["run", config, "--max-msgs", "3"], env={"SQLFLOW_INPUT_TOPIC": source, "SQLFLOW_OUTPUT_TOPIC": output})
    assert result.returncode == 0, result.stderr
    assert_records(stream.kafka.values(output), [{"n": 1, "name": "Austin"}, {"n": 2, "name": "Boston"}, {"n": 3, "name": None}])
    assert stream.kafka.committed(stream.group, source) == [3]
