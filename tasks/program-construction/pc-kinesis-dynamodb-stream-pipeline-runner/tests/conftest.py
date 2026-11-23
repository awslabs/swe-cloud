"""Shared fixtures: unique Kinesis streams / DynamoDB tables, program instances, observations."""

import datetime
import json
import os
import re
import signal
import tempfile
import time
import uuid
from pathlib import Path

import boto3
import pytest

import fixture_io
from harness import give_to_candidate, run_command, start_command, workspace

ENDPOINT = "http://engines:4566"
KINESIS = boto3.client("kinesis")
DYNAMO = boto3.client("dynamodb")

# Default timing used by most configs; tests wait on observed state, not fixed sleeps.
FAST = {"commit_period": "1s", "rebalance_period": "1s", "lease_period": "5s", "steal_grace_period": "1s"}

RFC3339 = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)$")


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def stamp(offset_seconds):
    """An RFC 3339 timestamp offset from now, as seeded lease rows use."""
    moment = now() + datetime.timedelta(seconds=offset_seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_time(text):
    match = RFC3339.match(text)
    assert match, f"not an RFC 3339 timestamp: {text!r}"
    fraction = (match.group(2) or ".0")[1:7].ljust(6, "0")
    zone = "+00:00" if match.group(3) == "Z" else match.group(3)
    return datetime.datetime.fromisoformat(f"{match.group(1)}.{fraction}{zone}")


def wait_for(condition, timeout, message, interval=0.2):
    """Poll until condition() is truthy; fail with the last observed value."""
    deadline = time.monotonic() + timeout
    value = None
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(interval)
    value = condition()
    if value:
        return value
    raise AssertionError(f"timed out after {timeout}s waiting for {message}; last observation: {value!r}")


def hash_key(index, count):
    """An explicit hash key inside shard `index` of a freshly created `count`-shard stream."""
    return str((2**128 // count) * index + 1)


# ---------------------------------------------------------------- YAML rendering


def _scalar(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if '"' in value and "'" not in value:
        return f"'{value}'"
    return json.dumps(value)


def render(tree, indent=0):
    """Render nested dicts as block YAML; lists of scalars become flow sequences."""
    lines = []
    pad = " " * indent
    for key, value in tree.items():
        if isinstance(value, dict):
            if value:
                lines.append(f"{pad}{key}:")
                lines.append(render(value, indent + 2))
            else:
                lines.append(f"{pad}{key}: {{}}")
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            lines.append(f"{pad}{key}:")
            for item in value:
                body = render(item, indent + 4).splitlines()
                lines.append(f"{pad}  - {body[0].lstrip()}")
                lines.extend(body[1:])
        elif isinstance(value, list):
            lines.append(f"{pad}{key}: [ {', '.join(_scalar(v) for v in value)} ]")
        elif isinstance(value, Block):
            lines.append(f"{pad}{key}: |")
            lines.extend(f"{pad}  {line}" for line in value.text.strip("\n").splitlines())
        else:
            lines.append(f"{pad}{key}: {_scalar(value)}")
    return "\n".join(lines)


class Block:
    """A literal block scalar (used for mapping bodies)."""

    def __init__(self, text):
        self.text = text


METADATA_MAPPING = Block(
    """
root.data = content().string()
root.stream = meta("kinesis_stream")
root.shard = meta("kinesis_shard")
root.partition_key = meta("kinesis_partition_key")
root.sequence = meta("kinesis_sequence_number")
"""
)


def connection():
    return {"endpoint": ENDPOINT, "region": "us-east-1", "credentials": {"id": "test", "secret": "test"}}


def kinesis_input(streams, table, create=True, **fields):
    settings = {"streams": streams, **connection(), "dynamodb": {"table": table, "create": create}}
    settings.update({**FAST, **fields})
    return {"aws_kinesis": settings}


def consumer_config(streams, table, out="out.jsonl", mapping=METADATA_MAPPING, output=None, create=True, **fields):
    config = {"http": {"enabled": False}, "input": kinesis_input(streams, table, create=create, **fields)}
    if mapping is not None:
        config["pipeline"] = {"processors": [{"mapping": mapping}]}
    config["output"] = output or {"file": {"path": out, "codec": "lines"}}
    return config


# ---------------------------------------------------------------- AWS observations


def scan(table):
    """All items as {(StreamID, ShardID): {attribute: (type, value)}}; None if the table is absent."""
    items, start = [], {}
    while True:
        try:
            page = DYNAMO.scan(TableName=table, ConsistentRead=True, **start)
        except DYNAMO.exceptions.ResourceNotFoundException:
            return None
        items += page.get("Items", [])
        if "LastEvaluatedKey" not in page:
            break
        start = {"ExclusiveStartKey": page["LastEvaluatedKey"]}
    rows = {}
    for item in items:
        row = {name: next(iter(value.items())) for name, value in item.items()}
        rows[(row["StreamID"][1], row["ShardID"][1])] = row
    return rows


def create_table(name):
    """A checkpoint table with the documented key schema, created by the test."""
    DYNAMO.create_table(
        TableName=name,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "StreamID", "AttributeType": "S"}, {"AttributeName": "ShardID", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "StreamID", "KeyType": "HASH"}, {"AttributeName": "ShardID", "KeyType": "RANGE"}],
    )
    DYNAMO.get_waiter("table_exists").wait(TableName=name, WaiterConfig={"Delay": 1, "MaxAttempts": 60})


def table_exists(table):
    try:
        DYNAMO.describe_table(TableName=table)
        return True
    except DYNAMO.exceptions.ResourceNotFoundException:
        return False


def final_row(stream, shard, sequence=None):
    row = {"StreamID": ("S", stream), "ShardID": ("S", shard)}
    if sequence is not None:
        row["SequenceNumber"] = ("S", sequence)
    return row


def assert_live(row, stream, shard, sequence, lease_seconds, slack=3.0):
    """A row leased by a running consumer: keys, ClientID, LeaseTimeout ≈ last write + lease."""
    expected = {"StreamID", "ShardID", "ClientID", "LeaseTimeout"} | ({"SequenceNumber"} if sequence is not None else set())
    assert row is not None, f"no row for {shard}"
    assert set(row) == expected, row
    assert all(kind == "S" for kind, _ in row.values()), row
    assert row["StreamID"][1] == stream and row["ShardID"][1] == shard, row
    if sequence is not None:
        assert row["SequenceNumber"][1] == sequence, row
    assert row["ClientID"][1], row
    remaining = (parse_time(row["LeaseTimeout"][1]) - now()).total_seconds()
    assert lease_seconds - slack <= remaining <= lease_seconds + 1.0, (remaining, row)
    return row["ClientID"][1]


def owner(row):
    return row.get("ClientID", (None, None))[1] if row else None


def read_records(stream):
    """Every record of every shard: [(shard_id, partition_key, data_bytes)] in shard order."""
    records = []
    for shard in KINESIS.list_shards(StreamName=stream)["Shards"]:
        iterator = KINESIS.get_shard_iterator(StreamName=stream, ShardId=shard["ShardId"], ShardIteratorType="TRIM_HORIZON")["ShardIterator"]
        while iterator:
            page = KINESIS.get_records(ShardIterator=iterator, Limit=10000)
            records += [(shard["ShardId"], r["PartitionKey"], r["Data"]) for r in page["Records"]]
            if not page["Records"]:
                break
            iterator = page.get("NextShardIterator")
    return records


# ---------------------------------------------------------------- fixtures


class Instance:
    """One background `program run CONFIG` process."""

    def __init__(self, process, stdout, stderr):
        self.process, self.stdout_path, self.stderr_path = process, stdout, stderr

    def running(self):
        return self.process.poll() is None

    def stop(self, timeout=40, sig=signal.SIGTERM):
        """Graceful shutdown (SIGTERM by default); returns the exit status."""
        if self.running():
            self.process.send_signal(sig)
        return self.process.wait(timeout=timeout)

    def kill(self):
        if self.running():
            self.process.kill()
        self.process.wait(timeout=30)

    def output(self):
        return Path(self.stdout_path).read_text(errors="replace"), Path(self.stderr_path).read_text(errors="replace")


class Lab:
    """Per-test resources: a candidate workspace, unique AWS names, running instances."""

    def __init__(self, root, logs):
        self.root, self.logs = root, logs
        self.tag = uuid.uuid4().hex[:10]
        self.streams, self.tables, self.instances = [], [], []

    # AWS resources
    def stream(self, shards, label="src"):
        name = f"{label}-{self.tag}-{len(self.streams)}"
        self.streams.append(name)
        KINESIS.create_stream(StreamName=name, ShardCount=shards)
        wait_for(lambda: KINESIS.describe_stream_summary(StreamName=name)["StreamDescriptionSummary"]["StreamStatus"] == "ACTIVE", 30, f"{name} ACTIVE")
        return name, sorted(s["ShardId"] for s in KINESIS.list_shards(StreamName=name)["Shards"])

    def table(self, create=False):
        name = f"ckpt-{self.tag}-{len(self.tables)}"
        self.tables.append(name)
        if create:
            create_table(name)
        return name

    def put(self, stream, data, partition_key, shard_index=None, shard_count=None, expect_shard=None):
        """put_record; returns a dict of the record as the consumer metadata should report it."""
        args = {"StreamName": stream, "Data": data.encode(), "PartitionKey": partition_key}
        if shard_index is not None:
            args["ExplicitHashKey"] = hash_key(shard_index, shard_count)
        result = KINESIS.put_record(**args)
        if expect_shard is not None:
            assert result["ShardId"] == expect_shard, result
        return {"data": data, "stream": stream, "shard": result["ShardId"], "partition_key": partition_key, "sequence": result["SequenceNumber"]}

    @staticmethod
    def seed(table, **attributes):
        DYNAMO.put_item(TableName=table, Item={k: {"S": str(v)} for k, v in attributes.items()})

    # files
    def write_config(self, name, config):
        text = config if isinstance(config, str) else render(config) + "\n"
        path = self.root / name
        fixture_io.write_text(path, text)
        return name

    def path(self, name):
        return self.root / name

    def lines(self, name):
        """Decoded JSON lines of a candidate-written output file ([] if absent)."""
        try:
            with fixture_io.open_file(self.root / name) as handle:
                text = handle.read()
        except FileNotFoundError:
            return []
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    # processes
    def start(self, config_name, env=None):
        index = len(self.instances)
        stdout, stderr = self.logs / f"stdout-{index}.txt", self.logs / f"stderr-{index}.txt"
        with open(stdout, "wb") as out, open(stderr, "wb") as err:
            process = start_command(["/app/program", "run", config_name], cwd=self.root, env=env, stdout=out, stderr=err)
        instance = Instance(process, stdout, stderr)
        self.instances.append(instance)
        return instance

    def close(self):
        for instance in self.instances:
            try:
                instance.kill()
            except Exception:  # noqa: BLE001 -- cleanup continues for every resource
                pass
        for name in self.streams:
            try:
                KINESIS.delete_stream(StreamName=name, EnforceConsumerDeletion=True)
            except Exception:  # noqa: BLE001
                pass
        for name in self.tables:
            try:
                DYNAMO.delete_table(TableName=name)
            except Exception:  # noqa: BLE001
                pass


@pytest.fixture
def lab():
    with workspace() as root, tempfile.TemporaryDirectory(prefix="logs-") as logs:
        os.chmod(logs, 0o700)
        environment = Lab(root, Path(logs))
        try:
            yield environment
        finally:
            environment.close()


def candidate_dir(lab, name):
    """A candidate-owned subdirectory of the workspace."""
    path = lab.root / name
    fixture_io.mkdir(path, parents=False, exist_ok=False)
    give_to_candidate(path)
    return path


def as_multiset(records):
    return sorted(json.dumps(r, sort_keys=True) for r in records)


def assert_string_fields(messages):
    for message in messages:
        assert isinstance(message, dict) and all(isinstance(v, str) for v in message.values()), message


def run_to_exit(lab, config_name, stdin=None, timeout=40, env=None):
    return run_command(["/app/program", "run", config_name], cwd=lab.root, input=stdin, capture_output=True, text=True, timeout=timeout, env=env)

