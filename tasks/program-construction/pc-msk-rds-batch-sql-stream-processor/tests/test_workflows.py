"""Sequences of runs and long-lived runs: commit-after-sink, resumption, drain and timed flushes.

Adapted from upstream tests/release/test_image.py
(test_lifecycle_drain_writes_the_buffered_batch_on_sigterm,
test_state_durability_survives_a_restart) and
internal/core/{offsets,drain}_test.go, internal/cli/run/flush_interval_test.go.
"""

import signal

import pytest

from streams import assert_distinct, assert_records, kafka_sink, pipeline, postgres_sink, same, source_block, wait_until
from test_windows import SIX, bucket, window_pipeline

BATCH_SQL = "SELECT n, kafka_offset, count(*) OVER () AS batch_rows FROM batch"


def row(n, offset, batch_rows):
    return {"n": n, "kafka_offset": offset, "batch_rows": batch_rows}


def test_reruns_resume_from_the_committed_offset(stream):
    source, output = stream.topic("in"), stream.topic("out")
    stream.kafka.produce(source, [{"n": i} for i in range(5)])
    env = {"SQLFLOW_INPUT_TOPIC": source, "SQLFLOW_OUTPUT_TOPIC": output}
    config = stream.config(pipeline(BATCH_SQL, kafka_sink(), batch_size=2))

    assert stream.run(["run", config, "--max-msgs", "3"], env=env).returncode == 0
    assert_records(stream.kafka.values(output), [row(0, 0, 2), row(1, 1, 2), row(2, 2, 1)])
    assert stream.kafka.committed(stream.group, source) == [3]

    assert stream.run(["run", config, "--max-msgs", "2"], env=env).returncode == 0
    assert stream.kafka.committed(stream.group, source) == [5]
    stream.kafka.produce(source, [{"n": 5}])
    assert stream.run(["run", "--config", config, "--max-msgs-to-process", "1"], env=env).returncode == 0
    assert_records(stream.kafka.values(output), [row(0, 0, 2), row(1, 1, 2), row(2, 2, 1), row(3, 3, 2), row(4, 4, 2), row(5, 5, 1)])
    assert stream.kafka.committed(stream.group, source) == [6]


def test_an_existing_group_offset_is_where_the_run_starts(stream):
    source, output = stream.topic("in"), stream.topic("out")
    stream.kafka.produce(source, [{"n": i} for i in range(4)])
    stream.kafka.commit(stream.group, source, [2])
    config = stream.config(pipeline(BATCH_SQL, kafka_sink(), batch_size=10))
    result = stream.run(["run", config, "--max-msgs", "2"], env={"SQLFLOW_INPUT_TOPIC": source, "SQLFLOW_OUTPUT_TOPIC": output})
    assert result.returncode == 0, result.stderr
    assert_records(stream.kafka.values(output), [row(2, 2, 2), row(3, 3, 2)])
    assert stream.kafka.committed(stream.group, source) == [4]


def test_latest_reset_skips_the_backlog(stream):
    source, output = stream.topic("in"), stream.topic("out")
    stream.kafka.produce(source, [{"n": 0}])
    config = stream.config(pipeline(BATCH_SQL, kafka_sink(), batch_size=1, source=source_block(reset="latest")))
    process = stream.start(["run", config, "--max-msgs", "1"], env={"SQLFLOW_INPUT_TOPIC": source, "SQLFLOW_OUTPUT_TOPIC": output})
    # Give the consumer time to join and settle on the end of the topic before the new record arrives.
    wait_until(lambda: process.poll() is not None, 12)
    stream.kafka.produce(source, [{"n": 1}])
    out, err = process.communicate(timeout=30)
    assert process.returncode == 0, err
    assert_records(stream.kafka.values(output), [row(1, 1, 1)])
    assert stream.kafka.committed(stream.group, source) == [2]


def test_a_refused_batch_is_redelivered_after_the_repair(stream):
    source = stream.topic("in")
    table = stream.table("uniq", "k text PRIMARY KEY, v bigint")
    stream.kafka.produce(source, ['{"k":"a","v":1}', '{"k":"b","v":2}', '{"k":"c","v":3}', '{"k":"a","v":4}', '{"k":"d","v":5}'])
    config = stream.config(pipeline("SELECT k, v FROM batch", postgres_sink(table, "append"), batch_size=2))
    env = {"SQLFLOW_INPUT_TOPIC": source}
    first = stream.run(["run", config, "--max-msgs", "5"], env=env)
    assert first.returncode == 10, first.stderr
    assert stream.sql(f'SELECT k, v FROM "{table}" ORDER BY k') == [("a", 1), ("b", 2)]
    assert stream.kafka.committed(stream.group, source) == [2]

    stream.sql(f"DELETE FROM \"{table}\" WHERE k = 'a'")
    second = stream.run(["run", config, "--max-msgs", "3"], env=env)
    assert second.returncode == 0, second.stderr
    assert stream.sql(f'SELECT k, v FROM "{table}" ORDER BY k') == [("a", 4), ("b", 2), ("c", 3), ("d", 5)]
    assert stream.kafka.committed(stream.group, source) == [5]


def test_a_corrected_handler_continues_after_a_raised_failure(stream):
    source, output = stream.topic("in"), stream.topic("out")
    stream.kafka.produce(source, ['{"k":"a","v":"1"}', '{"k":"b","v":"x"}', '{"k":"c","v":"3"}'])
    env = {"SQLFLOW_INPUT_TOPIC": source, "SQLFLOW_OUTPUT_TOPIC": output}
    strict = stream.config(pipeline("SELECT k, CAST(v AS BIGINT) + 1 AS v FROM batch", kafka_sink(), batch_size=1), "strict.yml")
    result = stream.run(["run", strict, "--max-msgs", "3"], env=env)
    assert result.returncode == 1, result.stderr
    assert stream.kafka.values(output) == [{"k": "a", "v": 2}]
    assert stream.kafka.committed(stream.group, source) == [1]

    lenient = stream.config(pipeline("SELECT k, TRY_CAST(v AS BIGINT) + 1 AS v FROM batch", kafka_sink(), batch_size=1), "lenient.yml")
    result = stream.run(["run", lenient, "--max-msgs", "2"], env=env)
    assert result.returncode == 0, result.stderr
    assert_records(stream.kafka.values(output), [{"k": "a", "v": 2}, {"k": "b", "v": None}, {"k": "c", "v": 4}])
    assert stream.kafka.committed(stream.group, source) == [3]


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_drains_the_buffered_batch(stream, sig):
    source, output = stream.topic("in"), stream.topic("out")
    stream.kafka.produce(source, [{"n": i} for i in range(4)])
    config = stream.config(pipeline(BATCH_SQL, kafka_sink(), batch_size=100))
    process = stream.start(["run", config, "--stats-json", "stats.json"], env={"SQLFLOW_INPUT_TOPIC": source, "SQLFLOW_OUTPUT_TOPIC": output})
    # The default flush interval outlasts this wait, so the four records stay buffered.
    wait_until(lambda: process.poll() is not None, 7)
    assert stream.kafka.read(output) == []
    assert stream.kafka.committed(stream.group, source) == [None]
    result = stream.stop(process, sig)
    assert result.returncode == 0, result.stderr
    assert_records(stream.kafka.values(output), [row(i, i, 4) for i in range(4)])
    assert stream.kafka.committed(stream.group, source) == [4]
    assert same(stream.stats(), {"messages_consumed": 4, "num_errors": 0})


def test_flush_interval_runs_partial_batches(stream):
    source, output = stream.topic("in"), stream.topic("out")
    stream.kafka.produce(source, [{"n": i} for i in range(3)])
    config = stream.config(pipeline(BATCH_SQL, kafka_sink(), batch_size=100, extra="  flush_interval_seconds: 2\n"))
    process = stream.start(["run", config, "--stats-json", "stats.json"], env={"SQLFLOW_INPUT_TOPIC": source, "SQLFLOW_OUTPUT_TOPIC": output})
    assert wait_until(lambda: len(stream.kafka.read(output)) >= 3, 15), "no timed flush"
    assert_records(stream.kafka.values(output), [row(0, 0, 3), row(1, 1, 3), row(2, 2, 3)])
    assert wait_until(lambda: stream.kafka.committed(stream.group, source) == [3], 10)

    stream.kafka.produce(source, [{"n": 3}])
    assert wait_until(lambda: len(stream.kafka.read(output)) >= 4, 15), "no second timed flush"
    assert stream.kafka.committed(stream.group, source) == [4]
    result = stream.stop(process)
    assert result.returncode == 0, result.stderr
    assert_records(stream.kafka.values(output), [row(0, 0, 3), row(1, 1, 3), row(2, 2, 3), row(3, 3, 1)])
    assert same(stream.stats(), {"messages_consumed": 4, "num_errors": 0})


def test_a_window_rerun_recounts_from_the_held_offset(stream):
    source, first, second = stream.topic("in"), stream.topic("out1"), stream.topic("out2")
    stream.kafka.produce(source, SIX)
    config = stream.config(window_pipeline())
    result = stream.run(["run", config, "--max-msgs", "4"], env={"SQLFLOW_INPUT_TOPIC": source, "SQLFLOW_OUTPUT_TOPIC": first})
    assert result.returncode == 0, result.stderr
    assert_distinct(stream.kafka.values(first), [bucket(0, "a", 2, 2), bucket(0, "b", 1, 1)])
    # The open 01:00 bucket's rows lived only in this run, so its source offsets stay uncommitted.
    assert stream.kafka.committed(stream.group, source) == [0]

    result = stream.run(["run", config, "--max-msgs", "6"], env={"SQLFLOW_INPUT_TOPIC": source, "SQLFLOW_OUTPUT_TOPIC": second})
    assert result.returncode == 0, result.stderr
    assert_distinct(stream.kafka.values(second), [bucket(0, "a", 3, 3), bucket(0, "b", 1, 1), bucket(1, "a", 1, 1)])
