"""Invocation: usage errors, rejected option values and setup failures, and options from the environment."""

import pytest
from harness import run_program
from lake import DLQ_FIELDS, quiet, unique, assert_rows_by

USAGE_ERRORS = {
    "no arguments": [],
    "missing table location": ["ingest", "some-topic"],
    "non-numeric latency": ["ingest", "some-topic", "s3://bucket/table", "--allowed_latency", "abc"],
    "negative batch size": ["ingest", "some-topic", "s3://bucket/table", "--max_messages_per_batch", "-1"],
    "unknown option": ["ingest", "some-topic", "s3://bucket/table", "--bogus"],
}


@pytest.mark.parametrize("args", USAGE_ERRORS.values(), ids=USAGE_ERRORS.keys())
def test_usage_errors_exit_2(args):
    result = run_program(args, timeout=20)
    assert result.returncode == 2, (result.stdout, result.stderr)


BAD_VALUES = {
    "transform without colon": ["-t", "nocolon"],
    "seek offsets not json": ["--seek_offsets", "xx"],
    "seek offsets partition not a number": ["--seek_offsets", '{"a":1}'],
    "unknown reset policy": ["--auto_offset_reset", "middle"],
    "kafka setting without equals": ["-K", "novalue"],
    "unknown kafka setting": ["-K", "no.such.property=1"],
    "invalid jmespath": ["-t", "x: foo[["],
}


@pytest.mark.parametrize("extra", BAD_VALUES.values(), ids=BAD_VALUES.keys())
def test_rejected_option_values_fail_without_writing(lake, extra):
    topic = lake.topic()
    table = lake.table([("id", "integer")])
    lake.produce(topic, [(0, {"id": 1})])
    before = table.keys()
    result = lake.run(lake.args(topic, table, *extra), timeout=40)
    assert result.returncode == 101, (result.stdout, result.stderr)
    assert table.keys() == before
    assert lake.lock_items(table) == []


def test_missing_tables_fail(lake):
    topic = lake.topic()
    lake.produce(topic, [(0, {"id": 1})])
    missing = lake.bare()
    result = lake.run(lake.args(topic, missing), timeout=40)
    assert result.returncode == 101, (result.stdout, result.stderr)
    assert missing.keys() == {}

    table = lake.table([("id", "integer")])
    missing_dlq = lake.bare()
    before = table.keys()
    result = lake.run(lake.args(topic, table, "--dlq_table_location", missing_dlq.uri), timeout=40)
    assert result.returncode == 101, (result.stdout, result.stderr)
    assert table.keys() == before and missing_dlq.keys() == {}


def test_options_from_environment_and_command_line_precedence(lake):
    topic = lake.topic()
    table = lake.table([("id", "integer"), ("off", "long")])
    lake.produce(topic, [(0, {"id": 0}), (0, {"id": 1})])
    env = {"KAFKA_TOPIC": topic, "TABLE_LOCATION": table.uri, "KAFKA_BROKERS": lake.brokers, "APP_ID": "from-env",
           "KAFKA_CONSUMER_GROUP": unique("group"), "ALLOWED_LATENCY": "3", "TRANSFORMS": "off: kafka.offset",
           "ENDS_AT_LATEST_OFFSETS": "true"}
    run = lake.start(["ingest"], env=env)
    table.wait_version(1)
    lake.produce(topic, [(0, {"id": 2})])
    assert run.wait(20) == 0, run.output()
    assert table.txns() == {"from-env-0": 1}
    assert_rows_by(table.rows(), [{"id": 0, "off": 0}, {"id": 1, "off": 1}], key="id")

    other = lake.table([("id", "integer"), ("off", "long")])
    env = {"APP_ID": "ignored", "ALLOWED_LATENCY": "60"}
    lake.start(lake.args(topic, other, "-t", "off: kafka.offset", app="from-cli", latency=2), env=env)
    other.wait_version(1, timeout=20)
    quiet(other, 1, 2)
    assert other.txns() == {"from-cli-0": 2}
    assert sorted(r["off"] for r in other.rows()) == [0, 1, 2]
