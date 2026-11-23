"""Invocation level: configuration validation, environment substitution, table creation, stdout output."""

import json
import time

import pytest

from conftest import DYNAMO, Block, as_multiset, consumer_config, create_table, run_to_exit, scan, table_exists, wait_for


def _invalid_case(name, stream):
    """Returns (config overrides, streams, diagnostic token) for one invalid configuration."""
    shard = "shardId-000000000000"
    return {
        "balanced_then_explicit": ({}, [stream, f"{stream}:{shard}"], "explicit"),
        "explicit_then_balanced": ({}, [f"{stream}:{shard}", stream], "explicit"),
        "comma_mixed": ({}, [f"{stream}:{shard},{stream}"], "explicit"),
        "two_shard_parts": ({}, [f"{stream}:a:b"], f"{stream}:a:b"),
        "poll_exceeds_lease": ({"poll_period": "6s"}, [stream], "poll_period"),
        "bad_duration": ({"lease_period": "fivesec"}, [stream], "fivesec"),
        "unknown_field": ({"lease_periodx": "5s"}, [stream], "lease_periodx"),
    }[name]


@pytest.mark.parametrize(
    "case",
    ["balanced_then_explicit", "explicit_then_balanced", "comma_mixed", "two_shard_parts", "poll_exceeds_lease", "bad_duration", "unknown_field"],
)
def test_invalid_config_exits_1_without_side_effects(lab, case):
    # Adapted from upstream input_test.go (TestStreamIDParser, poll_period > lease_period) and the
    # mixed balanced/explicit error of the reader constructor.
    stream, _ = lab.stream(1)
    table = lab.table()
    overrides, streams, token = _invalid_case(case, stream)
    lab.write_config("bad.yaml", consumer_config(streams, table, **overrides))
    result = run_to_exit(lab, "bad.yaml", timeout=30)
    assert result.returncode == 1, (result.returncode, result.stdout[-2000:], result.stderr[-2000:])
    assert token in result.stdout + result.stderr, (result.stdout[-2000:], result.stderr[-2000:])
    assert not table_exists(table), "an invalid config must not create the checkpoint table"
    assert not lab.path("out.jsonl").exists()


def test_poll_period_equal_to_lease_period_is_accepted(lab):
    stream, shards = lab.stream(1)
    table = lab.table()
    record = lab.put(stream, "only", "pk-1")
    lab.write_config("c.yaml", consumer_config([stream], table, poll_period="5s"))
    instance = lab.start("c.yaml")
    wait_for(lambda: lab.lines("out.jsonl"), 20, "the record to be emitted")
    assert instance.running()
    assert instance.stop() == 0
    assert lab.lines("out.jsonl") == [record]


def test_env_substitution_and_stdout_output_carries_only_messages(lab):
    stream, shards = lab.stream(1)
    table = lab.table()
    sent = [lab.put(stream, f"m{i}", f"key-{i}") for i in range(3)]
    config = consumer_config(
        ["${SOURCE_STREAM:" + stream + "}"],
        "${CHECKPOINT_TABLE}",
        mapping=None,
        output={"stdout": {"codec": "lines"}},
    )
    config["pipeline"] = {"processors": [{"mapping": _mapping_value_key()}]}
    lab.write_config("c.yaml", config)
    instance = lab.start("c.yaml", env={"CHECKPOINT_TABLE": table})
    wait_for(lambda: len(_stdout_lines(instance)) >= 3, 20, "three messages on stdout")
    wait_for(lambda: len(scan(table) or {}) == 1, 10, "a row in the table named by ${CHECKPOINT_TABLE}")
    assert instance.stop() == 0
    decoded = [json.loads(line) for line in _stdout_lines(instance)]
    assert as_multiset(decoded) == as_multiset({"value": r["data"], "key": r["partition_key"]} for r in sent)


def _mapping_value_key():
    return Block('root.value = content().string()\nroot.key = meta("kinesis_partition_key")\n')


def _stdout_lines(instance):
    return [line for line in instance.output()[0].splitlines() if line.strip()]


@pytest.mark.parametrize("billing", ["default", "provisioned"])
def test_created_table_schema(lab, billing):
    stream, shards = lab.stream(1)
    table = lab.table()
    dynamodb = {"table": table, "create": True}
    if billing == "provisioned":
        dynamodb.update(billing_mode="PROVISIONED", read_capacity_units=3, write_capacity_units=4)
    config = consumer_config([stream], table)
    config["input"]["aws_kinesis"]["dynamodb"] = dynamodb
    lab.write_config("c.yaml", config)
    instance = lab.start("c.yaml")
    wait_for(lambda: table_exists(table), 20, "the table to be created")
    description = DYNAMO.describe_table(TableName=table)["Table"]
    assert sorted((k["AttributeName"], k["KeyType"]) for k in description["KeySchema"]) == [("ShardID", "RANGE"), ("StreamID", "HASH")]
    assert sorted((a["AttributeName"], a["AttributeType"]) for a in description["AttributeDefinitions"]) == [("ShardID", "S"), ("StreamID", "S")]
    if billing == "provisioned":
        throughput = description["ProvisionedThroughput"]
        assert (throughput["ReadCapacityUnits"], throughput["WriteCapacityUnits"]) == (3, 4)
    else:
        assert description.get("BillingModeSummary", {}).get("BillingMode") == "PAY_PER_REQUEST", description
    assert instance.stop() == 0


def test_missing_table_without_create_waits_for_it(lab):
    stream, shards = lab.stream(1)
    table = lab.table()
    record = lab.put(stream, "late", "pk")
    lab.write_config("c.yaml", consumer_config([stream], table, create=False))
    instance = lab.start("c.yaml")
    time.sleep(4)
    assert instance.running(), "the program keeps retrying while the table is missing"
    assert not table_exists(table)
    assert lab.lines("out.jsonl") == []
    create_table(table)
    wait_for(lambda: lab.lines("out.jsonl"), 25, "consumption once the table exists")
    assert instance.stop() == 0
    assert lab.lines("out.jsonl") == [record]
