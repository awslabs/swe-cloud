"""The Kinesis output fed from stdin: record data, partition/hash key interpolation, batching."""

import json

import pytest

from conftest import KINESIS, connection, read_records, run_to_exit


def producer_config(stream, **output):
    return {
        "http": {"enabled": False},
        "input": {"stdin": {"scanner": {"lines": {}}}},
        "output": {"aws_kinesis": {"stream": stream, **connection(), **output}},
    }


def test_partition_and_hash_key_interpolation(lab):
    # Adapted from the upstream integration template (output batching + interpolated partition key).
    stream, shards = lab.stream(2, "dst")
    lines = [json.dumps({"key": f"k{i}", "h": "0" if i % 3 == 0 else str(2**127 + 1), "n": str(i)}) for i in range(7)]
    lab.write_config(
        "p.yaml",
        producer_config(stream, partition_key='user-${! json("key") }', hash_key='${! json("h") }', max_in_flight=1, batching={"count": 3, "period": "200ms"}),
    )
    result = run_to_exit(lab, "p.yaml", stdin="\n".join(lines) + "\n")
    assert result.returncode == 0, result.stderr[-2000:]
    expected = [(shards[0] if i % 3 == 0 else shards[1], f"user-k{i}", line.encode()) for i, line in enumerate(lines)]
    observed = read_records(stream)
    for shard in shards:
        assert [r for r in observed if r[0] == shard] == [r for r in expected if r[0] == shard], "per-shard order follows input order"
    assert sorted(observed) == sorted(expected)


@pytest.mark.parametrize("hash_key", ["absent", "empty"])
def test_partition_key_alone_routes_records(lab, hash_key):
    stream, _ = lab.stream(4, "dst")
    oracle, _ = lab.stream(4, "oracle")
    values = [f"value-{i}" for i in range(8)]
    output = {"partition_key": "${! content() }"}
    if hash_key == "empty":
        output["hash_key"] = ""
    lab.write_config("p.yaml", producer_config(stream, **output))
    result = run_to_exit(lab, "p.yaml", stdin="".join(v + "\n" for v in values))
    assert result.returncode == 0, result.stderr[-2000:]
    # The emulator's placement of the same partition key, with no explicit hash key.
    placement = {v: KINESIS.put_record(StreamName=oracle, Data=b"x", PartitionKey=v)["ShardId"] for v in values}
    assert sorted(read_records(stream)) == sorted((placement[v], v, v.encode()) for v in values)


def test_large_batch_is_delivered_completely_and_in_order(lab):
    stream, shards = lab.stream(1, "dst")
    lab.write_config("p.yaml", producer_config(stream, partition_key="fixed", max_in_flight=1, batching={"count": 1200}))
    result = run_to_exit(lab, "p.yaml", stdin="".join(f"record-{i:04d}\n" for i in range(1200)), timeout=50)
    assert result.returncode == 0, result.stderr[-2000:]
    assert read_records(stream) == [(shards[0], "fixed", f"record-{i:04d}".encode()) for i in range(1200)]


def test_literal_text_around_interpolations(lab):
    stream, shards = lab.stream(1, "dst")
    lab.write_config("p.yaml", producer_config(stream, partition_key='pre-${! json("a") }-mid-${! json("b") }-post'))
    lines = ['{"a":"x","b":"y"}', '{"a":"1","b":"2"}']
    result = run_to_exit(lab, "p.yaml", stdin="\n".join(lines) + "\n")
    assert result.returncode == 0, result.stderr[-2000:]
    assert sorted(read_records(stream)) == sorted([(shards[0], "pre-x-mid-y-post", lines[0].encode()), (shards[0], "pre-1-mid-2-post", lines[1].encode())])

