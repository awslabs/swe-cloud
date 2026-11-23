"""Tumbling windows: watermark closes, grace, idleness, late rows and where closed buckets go.

Adapted from upstream dev/config/examples/{tumbling.window.yml,kafka.stateful.window.yml},
dev/fixtures/window.jsonl, docs/windows/decisions.md and
internal/core/{watermarks,windowoffsets}_test.go, internal/cli/run/{idle_close,window_refusal}_test.go.

Window output is compared as a set of distinct records (see /docs/interface.md):
a closed bucket may be published more than once around shutdown.
"""

import pytest

from streams import assert_distinct, kafka_sink, pipeline, postgres_sink, same, source_block, wait_until, window_tables

INSERT_SQL = """
INSERT INTO agg BY NAME
SELECT time_bucket(INTERVAL '1 hour', event_time) AS bucket, properties.city AS city, count(*) AS count
FROM batch GROUP BY bucket, city
"""
EMIT = "SELECT bucket, city, sum(count) AS count, count(*) AS parts FROM closed GROUP BY ALL"
NOOP = "  sink:\n    type: noop\n"
EVENT_TIME = "{path: timestamp, format: rfc3339}"


def rec(clock, city):
    return {"timestamp": f"2026-01-01T{clock}Z", "properties": {"city": city}}


def bucket(hour, city, count, parts):
    return {"bucket": f"2026-01-01T{hour:02d}:00:00Z", "city": city, "count": str(count), "parts": parts}


def window_pipeline(*, batch_size=2, flush=30, emit=EMIT, sink=None, **window):
    tables = window_tables(emit=emit, sink=sink or kafka_sink(indent="        "), **window)
    return pipeline(INSERT_SQL, NOOP, batch_size=batch_size, tables=tables, extra=f"  flush_interval_seconds: {flush}\n",
                    source=source_block(event_time=EVENT_TIME))


SIX = [rec("00:10:00", "a"), rec("00:20:00", "b"), rec("00:30:00", "a"), rec("01:05:00", "a"), rec("00:40:00", "a"),
       rec("02:30:00", "b")]


def test_watermark_closes_buckets_it_has_passed(stream):
    source, output = stream.topic("in"), stream.topic("out")
    stream.kafka.produce(source, SIX)
    config = stream.config(window_pipeline())
    result = stream.run(["run", config, "--max-msgs", "6", "--stats-json", "stats.json"],
                        env={"SQLFLOW_INPUT_TOPIC": source, "SQLFLOW_OUTPUT_TOPIC": output})
    assert result.returncode == 0, result.stderr
    # 02:30 moved the watermark past 01:00 and 02:00 closed; the 02:00 bucket is still open.
    assert_distinct(stream.kafka.values(output), [bucket(0, "a", 3, 3), bucket(0, "b", 1, 1), bucket(1, "a", 1, 1)])
    assert same(stream.stats(), {"messages_consumed": 6, "num_errors": 0})
    # The open bucket is fed from offset 5 onwards, and nothing before the oldest retained row is committed.
    assert stream.kafka.committed(stream.group, source) == [0]


def test_default_emit_publishes_the_window_rows(stream):
    source, output = stream.topic("in"), stream.topic("out")
    stream.kafka.produce(source, SIX[:4])
    config = stream.config(window_pipeline(emit="SELECT * FROM closed"))
    result = stream.run(["run", config, "--max-msgs", "4"], env={"SQLFLOW_INPUT_TOPIC": source, "SQLFLOW_OUTPUT_TOPIC": output})
    assert result.returncode == 0, result.stderr
    rows = stream.kafka.values(output)
    assert_distinct(rows, [{"bucket": "2026-01-01T00:00:00Z", "city": "a", "count": 1},
                           {"bucket": "2026-01-01T00:00:00Z", "city": "b", "count": 1}])


def test_grace_delays_the_close(stream):
    source = stream.topic("in")
    stream.kafka.produce(source, [rec("00:10:00", "a"), rec("01:20:00", "b"), rec("01:35:00", "a"), rec("01:55:00", "c")])
    cases = [(0, 3, [bucket(0, "a", 1, 1)]), (3000, 3, []), (3000, 4, [bucket(0, "a", 1, 1)])]
    for i, (grace, count, expected) in enumerate(cases):
        output = stream.topic(f"out{i}")
        stream.group = f"{stream.prefix}-g{i}"
        config = stream.config(window_pipeline(batch_size=1, grace=grace), f"grace{i}.yml")
        result = stream.run(["run", config, "--max-msgs", str(count)], env={"SQLFLOW_INPUT_TOPIC": source, "SQLFLOW_OUTPUT_TOPIC": output})
        assert result.returncode == 0, result.stderr
        assert_distinct(stream.kafka.values(output), expected)


def test_records_without_a_placeable_time_are_refused(stream):
    source, output = stream.topic("in"), stream.topic("out")
    stream.kafka.produce(source, [rec("00:10:00", "a"), {"properties": {"city": "n"}},
                                  {"timestamp": "garbage", "properties": {"city": "g"}}, rec("01:30:00", "a")])
    config = stream.config(window_pipeline(batch_size=1, flush=1))
    result = stream.run(["run", config, "--max-msgs", "4", "--stats-json", "stats.json"],
                        env={"SQLFLOW_INPUT_TOPIC": source, "SQLFLOW_OUTPUT_TOPIC": output})
    assert result.returncode == 0, result.stderr
    assert_distinct(stream.kafka.values(output), [bucket(0, "a", 1, 1)])
    assert same(stream.stats(), {"messages_consumed": 4, "num_errors": 0})


def test_idle_close_publishes_every_open_bucket(stream):
    source, output = stream.topic("in"), stream.topic("out")
    stream.kafka.produce(source, SIX)
    config = stream.config(window_pipeline(idle=2, flush=1))
    process = stream.start(["run", config, "--stats-json", "stats.json"],
                           env={"SQLFLOW_INPUT_TOPIC": source, "SQLFLOW_OUTPUT_TOPIC": output})
    expected = [bucket(0, "a", 3, 3), bucket(0, "b", 1, 1), bucket(1, "a", 1, 1), bucket(2, "b", 1, 1)]
    wait_until(lambda: len({r["bucket"] for r in stream.kafka.values(output)}) >= 3, 25)
    assert_distinct(stream.kafka.values(output), expected)
    assert wait_until(lambda: stream.kafka.committed(stream.group, source) == [6], 15)
    result = stream.stop(process)
    assert result.returncode == 0, result.stderr
    assert_distinct(stream.kafka.values(output), expected)
    assert same(stream.stats(), {"messages_consumed": 6, "num_errors": 0})


def test_late_rows_for_a_closed_bucket_are_refused(stream):
    source, output = stream.topic("in"), stream.topic("out")
    stream.kafka.produce(source, [rec("00:10:00", "a"), rec("00:20:00", "b"), rec("01:05:00", "a")])
    config = stream.config(window_pipeline(batch_size=1, flush=1))
    process = stream.start(["run", config, "--stats-json", "stats.json"],
                           env={"SQLFLOW_INPUT_TOPIC": source, "SQLFLOW_OUTPUT_TOPIC": output})
    first = [bucket(0, "a", 1, 1), bucket(0, "b", 1, 1)]
    assert wait_until(lambda: len(stream.kafka.values(output)) >= 2, 20), "the 00:00 bucket never closed"
    assert_distinct(stream.kafka.values(output), first)
    assert wait_until(lambda: stream.kafka.committed(stream.group, source) == [2], 10)

    stream.kafka.produce(source, [rec("00:50:00", "a"), rec("01:10:00", "b"), rec("02:01:00", "c")])
    later = first + [bucket(1, "a", 1, 1), bucket(1, "b", 1, 1)]
    assert wait_until(lambda: any(r["bucket"].startswith("2026-01-01T01") for r in stream.kafka.values(output)), 20)
    assert wait_until(lambda: stream.kafka.committed(stream.group, source) == [5], 10)
    result = stream.stop(process)
    assert result.returncode == 0, result.stderr
    # The late 00:50 row neither changed nor republished the closed bucket; 02:00 never closed.
    assert_distinct(stream.kafka.values(output), later)
    assert same(stream.stats(), {"messages_consumed": 6, "num_errors": 0})


def test_a_silent_partition_holds_the_watermark(stream):
    source, output = stream.topic("in", partitions=2), stream.topic("out")
    stream.kafka.produce(source, [rec("00:10:00", "a"), rec("01:10:00", "a"), rec("02:10:00", "a")], partition=0)
    config = stream.config(window_pipeline(batch_size=1, flush=1))
    process = stream.start(["run", config], env={"SQLFLOW_INPUT_TOPIC": source, "SQLFLOW_OUTPUT_TOPIC": output})
    assert wait_until(lambda: stream.kafka.committed(stream.group, source, partitions=2) == [0, None], 15)
    wait_until(lambda: stream.kafka.values(output), 5)
    assert stream.kafka.values(output) == []

    stream.kafka.produce(source, [rec("01:30:00", "b")], partition=1)
    assert wait_until(lambda: stream.kafka.values(output), 20), "the partition's record did not move the watermark"
    assert wait_until(lambda: stream.kafka.committed(stream.group, source, partitions=2) == [1, 0], 10)
    result = stream.stop(process)
    assert result.returncode == 0, result.stderr
    assert_distinct(stream.kafka.values(output), [bucket(0, "a", 1, 1)])


def test_kafka_record_timestamp_is_the_default_event_time(stream):
    source, output = stream.topic("in"), stream.topic("out")
    base = 1767225600000  # 2026-01-01T00:00:00Z in milliseconds
    stream.kafka.produce(source, ['{"city":"a"}', '{"city":"b"}', '{"city":"a"}'],
                         timestamps=[base + 600_000, base + 1_200_000, base + 3_900_000])
    sql = ("INSERT INTO agg BY NAME SELECT time_bucket(INTERVAL '1 hour', event_time) AS bucket, city, count(*) AS count "
           "FROM batch GROUP BY ALL")
    tables = window_tables(emit="SELECT strftime(bucket, '%H:%M') AS bucket, city, sum(count) AS count FROM closed GROUP BY ALL",
                           sink=kafka_sink(indent="        "))
    config = stream.config(pipeline(sql, NOOP, batch_size=1, tables=tables, extra="  flush_interval_seconds: 1\n"))
    process = stream.start(["run", config], env={"SQLFLOW_INPUT_TOPIC": source, "SQLFLOW_OUTPUT_TOPIC": output})
    assert wait_until(lambda: len(stream.kafka.values(output)) >= 2, 20)
    result = stream.stop(process)
    assert result.returncode == 0, result.stderr
    assert_distinct(stream.kafka.values(output), [{"bucket": "00:00", "city": "a", "count": "1"},
                                                  {"bucket": "00:00", "city": "b", "count": "1"}])


def test_lateness_republishes_the_whole_bucket_to_an_upsert_sink(stream):
    source = stream.topic("in")
    table = stream.table("wagg", "bucket timestamptz NOT NULL, city text NOT NULL, count bigint, PRIMARY KEY (bucket, city)")
    base = 1767225600  # 2026-01-01T00:00:00Z
    sql = ("INSERT INTO agg BY NAME SELECT time_bucket(INTERVAL '1 hour', event_time) AS bucket, city, count(*) AS count "
           "FROM batch GROUP BY ALL")
    tables = window_tables(lateness=7200, emit="SELECT bucket, city, sum(count)::BIGINT AS count FROM closed GROUP BY ALL",
                           sink=postgres_sink(table, "upsert", ["bucket", "city"], indent="        "))
    config = stream.config(pipeline(sql, NOOP, batch_size=1, tables=tables, extra="  flush_interval_seconds: 1\n",
                                    source=source_block(event_time="{path: ts, format: unix_s}")))

    def rows():
        return stream.sql(f"SELECT to_char(bucket AT TIME ZONE 'UTC', 'HH24:MI'), city, count FROM \"{table}\" ORDER BY 1, 2")

    stream.kafka.produce(source, [{"ts": base + 600, "city": "a"}, {"ts": base + 1200, "city": "a"}, {"ts": base + 3900, "city": "b"}])
    process = stream.start(["run", config], env={"SQLFLOW_INPUT_TOPIC": source})
    assert wait_until(lambda: rows() == [("00:00", "a", 2)], 20), rows()

    # Within the lateness the bucket is recounted whole, and a new key appears in it.
    stream.kafka.produce(source, [{"ts": base + 1800, "city": "a"}, {"ts": base + 1900, "city": "c"}])
    assert wait_until(lambda: rows() == [("00:00", "a", 3), ("00:00", "c", 1)], 20), rows()

    # 03:01 closes 01:00 and ends the lateness of 00:00; a row for 00:00 after that is refused.
    stream.kafka.produce(source, [{"ts": base + 3 * 3600 + 60, "city": "d"}])
    assert wait_until(lambda: ("01:00", "b", 1) in rows(), 20), rows()
    stream.kafka.produce(source, [{"ts": base + 1950, "city": "a"}])
    wait_until(lambda: False, 4)
    result = stream.stop(process)
    assert result.returncode == 0, result.stderr
    assert rows() == [("00:00", "a", 3), ("00:00", "c", 1), ("01:00", "b", 1)]


@pytest.mark.parametrize("sink", ["kafka", "append"])
def test_run_refuses_lateness_with_an_appending_window_sink(stream, sink):
    source = stream.topic("in")
    stream.kafka.produce(source, [rec("00:10:00", "a")])
    table = stream.table("w", "bucket timestamptz, city text, count bigint")
    window_sink = kafka_sink(stream.prefix + "-never", indent="        ") if sink == "kafka" else \
        postgres_sink(table, "append", indent="        ")
    config = stream.config(window_pipeline(lateness=60, sink=window_sink))
    result = stream.run(["run", config, "--max-msgs", "1"], env={"SQLFLOW_INPUT_TOPIC": source})
    assert result.returncode == 10, result.stderr
    line = next(line for line in result.stderr.splitlines() if line.startswith("Error: "))
    assert line.startswith("Error: [user.config.invalid] ") and "allowed_lateness_seconds" in line
    assert stream.kafka.committed(stream.group, source) == [None]
