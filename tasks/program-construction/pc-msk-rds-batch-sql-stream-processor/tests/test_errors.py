"""on_error policies: malformed records and failing handler SQL under RAISE, IGNORE and DLQ.

Adapted from upstream tests/release/test_image.py
(test_error_ignore_drops_bad_records_and_keeps_going,
test_error_dlq_diverts_a_record_the_handler_cannot_parse,
test_error_dlq_diverts_a_batch_the_handler_cannot_query) and
dev/config/examples/kafka.dlq.yml.
"""

import re
from datetime import datetime, timezone

import pytest

from streams import assert_records, decode, dlq_block, kafka_sink, pipeline, same

MIXED = ['{"k":"a","v":1}', "not json", '{"k":"b","v":2}', '{"k":"c"', '{"k":"d","v":4}']
# RFC 3339 in UTC: a Z or a zero offset, with any fraction of a second.
TIMESTAMP = re.compile(r"^(\d{4}-\d\d-\d\d)[Tt](\d\d:\d\d:\d\d)(\.\d+)?([Zz]|[+-]00:00)$")


def error_line(stderr):
    return next((line for line in stderr.splitlines() if line.startswith("Error: ")), "")


def check_envelope(record, started):
    assert set(record) == {"error", "message", "phase", "timestamp"}, record
    assert all(type(v) is str for v in record.values()), record
    match = TIMESTAMP.match(record["timestamp"])
    assert match, record["timestamp"]
    when = datetime.fromisoformat(f"{match[1]}T{match[2]}{(match[3] or '')[:7]}+00:00")
    assert abs((when - started).total_seconds()) < 120, record["timestamp"]


def run_mixed(stream, policy, batch_size=2, values=MIXED, sql="SELECT k, v, kafka_offset FROM batch"):
    source, output, dlq = stream.topic("in"), stream.topic("out"), stream.topic("dlq")
    stream.kafka.produce(source, values)
    config = stream.config(pipeline(sql, kafka_sink(), batch_size=batch_size, extra=dlq_block(policy)))
    started = datetime.now(timezone.utc)
    result = stream.run(["run", config, "--max-msgs", str(len(values)), "--stats-json", "stats.json"],
                        env={"SQLFLOW_INPUT_TOPIC": source, "SQLFLOW_OUTPUT_TOPIC": output, "SQLFLOW_DLQ_TOPIC": dlq})
    return result, source, output, dlq, started


GOOD_ROWS = [{"k": "a", "v": 1, "kafka_offset": 0}, {"k": "b", "v": 2, "kafka_offset": 2}, {"k": "d", "v": 4, "kafka_offset": 4}]


def test_dlq_receives_each_malformed_record(stream):
    result, source, output, dlq, started = run_mixed(stream, "DLQ")
    assert result.returncode == 0, result.stderr
    assert_records(stream.kafka.values(output), GOOD_ROWS)
    letters = stream.kafka.values(dlq)
    assert len(letters) == 2
    for letter in letters:
        check_envelope(letter, started)
    assert sorted((l["message"], l["phase"], l["error"]) for l in letters) == [
        ("not json", "handler.write", '[user.data.malformed] invalid json: "not json"'),
        ('{"k":"c"', "handler.write", '[user.data.malformed] invalid json: "{\\"k\\":\\"c\\""'),
    ]
    assert same(stream.stats(), {"messages_consumed": 5, "num_errors": 2})
    assert stream.kafka.committed(stream.group, source) == [5]


def test_batches_fill_with_readable_records_only(stream):
    sql = "SELECT k, count(*) OVER () AS batch_rows, min(kafka_offset) OVER () AS first_offset FROM batch"
    result, source, output, dlq, _ = run_mixed(stream, "IGNORE", sql=sql)
    assert result.returncode == 0, result.stderr
    assert_records(stream.kafka.values(output), [
        {"k": "a", "batch_rows": 2, "first_offset": 0}, {"k": "b", "batch_rows": 2, "first_offset": 0},
        {"k": "d", "batch_rows": 1, "first_offset": 4}])


def test_ignore_drops_malformed_records(stream):
    result, source, output, dlq, _ = run_mixed(stream, "IGNORE")
    assert result.returncode == 0, result.stderr
    assert_records(stream.kafka.values(output), GOOD_ROWS)
    assert stream.kafka.read(dlq) == []
    assert same(stream.stats(), {"messages_consumed": 5, "num_errors": 2})
    assert stream.kafka.committed(stream.group, source) == [5]


def test_raise_stops_at_a_malformed_record(stream):
    result, source, output, dlq, _ = run_mixed(stream, "RAISE")
    assert result.returncode == 10, result.stderr
    assert error_line(result.stderr) == 'Error: [user.data.malformed] invalid json: "not json"'
    assert stream.kafka.read(output) == []
    assert stream.kafka.read(dlq) == []
    assert stream.kafka.committed(stream.group, source) == [None]
    assert not (stream.root / "stats.json").exists()


def test_raise_is_the_default_policy(stream):
    source, output = stream.topic("in"), stream.topic("out")
    stream.kafka.produce(source, ['{"k":"a"}', "{oops", '{"k":"b"}'])
    config = stream.config(pipeline("SELECT k FROM batch", kafka_sink(), batch_size=1))
    result = stream.run(["run", config, "--max-msgs", "3"], env={"SQLFLOW_INPUT_TOPIC": source, "SQLFLOW_OUTPUT_TOPIC": output})
    assert result.returncode == 10, result.stderr
    assert error_line(result.stderr).startswith("Error: [user.data.malformed] ")
    # The first batch was written and committed before the bad record was read.
    assert stream.kafka.values(output) == [{"k": "a"}]
    assert stream.kafka.committed(stream.group, source) == [1]


def test_a_dropped_tail_record_is_not_committed(stream):
    values = ['{"k":"a","v":1}', '{"k":"b","v":2}', "bad"]
    result, source, output, dlq, _ = run_mixed(stream, "IGNORE", values=values)
    assert result.returncode == 0, result.stderr
    assert_records(stream.kafka.values(output), [{"k": "a", "v": 1, "kafka_offset": 0}, {"k": "b", "v": 2, "kafka_offset": 1}])
    assert same(stream.stats(), {"messages_consumed": 3, "num_errors": 1})
    assert stream.kafka.committed(stream.group, source) == [2]


QUERY_ERROR_SQL = "SELECT k, nonexistent_col FROM batch"
THREE = ['{"k":"a","v":1}', '{"k":"b","v":2}', '{"k":"c","v":3}']


def test_dlq_receives_one_letter_per_failed_batch(stream):
    result, source, output, dlq, started = run_mixed(stream, "DLQ", values=THREE, sql=QUERY_ERROR_SQL)
    assert result.returncode == 0, result.stderr
    assert stream.kafka.read(output) == []
    letters = stream.kafka.values(dlq)
    assert len(letters) == 2
    for letter in letters:
        check_envelope(letter, started)
        assert letter["message"] == "Handler invocation failed"
        assert letter["phase"] == "handler.invoke"
        assert "nonexistent_col" in letter["error"] and "[user." not in letter["error"]
    assert same(stream.stats(), {"messages_consumed": 3, "num_errors": 2})
    assert stream.kafka.committed(stream.group, source) == [3]


def test_ignore_skips_failed_batches(stream):
    result, source, output, dlq, _ = run_mixed(stream, "IGNORE", values=THREE, sql=QUERY_ERROR_SQL)
    assert result.returncode == 0, result.stderr
    assert stream.kafka.read(output) == []
    assert stream.kafka.read(dlq) == []
    assert same(stream.stats(), {"messages_consumed": 3, "num_errors": 2})
    assert stream.kafka.committed(stream.group, source) == [3]


def test_raise_on_query_error_exits_one(stream):
    result, source, output, dlq, _ = run_mixed(stream, "RAISE", values=THREE, sql=QUERY_ERROR_SQL)
    assert result.returncode == 1, result.stderr
    assert "nonexistent_col" in result.stderr
    assert stream.kafka.read(output) == [] and stream.kafka.read(dlq) == []
    assert stream.kafka.committed(stream.group, source) == [None]


def test_only_the_failing_batch_is_dead_lettered(stream):
    values = ['{"k":"1","v":1}', '{"k":"2","v":2}', '{"k":"x","v":3}', '{"k":"4","v":4}', '{"k":"5","v":5}']
    result, source, output, dlq, started = run_mixed(stream, "DLQ", values=values, sql="SELECT CAST(k AS INTEGER) AS k, v FROM batch")
    assert result.returncode == 0, result.stderr
    assert_records(stream.kafka.values(output), [{"k": 1, "v": 1}, {"k": 2, "v": 2}, {"k": 5, "v": 5}])
    [letter] = stream.kafka.values(dlq)
    check_envelope(letter, started)
    assert (letter["message"], letter["phase"]) == ("Handler invocation failed", "handler.invoke")
    assert letter["error"] and "[user." not in letter["error"]
    assert same(stream.stats(), {"messages_consumed": 5, "num_errors": 1})
    assert stream.kafka.committed(stream.group, source) == [5]


def test_conflicting_field_types_fail_the_batch(stream):
    source, output = stream.topic("in"), stream.topic("out")
    stream.kafka.produce(source, ['{"v":"s"}', '{"v":1}'])
    config = stream.config(pipeline("SELECT v FROM batch", kafka_sink(), batch_size=2))
    result = stream.run(["run", config, "--max-msgs", "2", "--stats-json", "stats.json"],
                        env={"SQLFLOW_INPUT_TOPIC": source, "SQLFLOW_OUTPUT_TOPIC": output})
    assert result.returncode == 1, result.stderr
    assert stream.kafka.read(output) == []
    assert stream.kafka.committed(stream.group, source) == [None]
    assert not (stream.root / "stats.json").exists()


@pytest.mark.parametrize(("policy", "exit_status"), [("RAISE", 1), ("DLQ", 0)])
def test_disk_batch_malformed_records(stream, policy, exit_status):
    values = ['{"k":"a"}', "nope", '{"k":"b"}']
    source, output, dlq = stream.topic("in"), stream.topic("out"), stream.topic("dlq")
    stream.kafka.produce(source, values)
    config = stream.config(pipeline("SELECT k FROM batch", kafka_sink(), batch_size=5, extra=dlq_block(policy),
                                    handler="handlers.InferredDiskBatch"))
    started = datetime.now(timezone.utc)
    result = stream.run(["run", config, "--max-msgs", "3"],
                        env={"SQLFLOW_INPUT_TOPIC": source, "SQLFLOW_OUTPUT_TOPIC": output, "SQLFLOW_DLQ_TOPIC": dlq,
                             "SQLFLOW_SQL_RESULTS_CACHE_DIR": str(stream.root / "cache")})
    assert result.returncode == exit_status, result.stderr
    if policy == "RAISE":
        assert stream.kafka.read(output) == []
        assert stream.kafka.committed(stream.group, source) == [None]
        return
    assert_records(stream.kafka.values(output), [{"k": "a"}, {"k": "b"}])
    [letter] = [decode(r["value"]) for r in stream.kafka.read(dlq)]
    check_envelope(letter, started)
    assert (letter["message"], letter["phase"], letter["error"]) == ("nope", "handler.write", 'invalid json: "nope"')
    assert stream.kafka.committed(stream.group, source) == [3]
