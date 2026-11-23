"""Shared fixtures for the ingest tests: Kafka topics, Delta tables in S3, program runs.

Everything here creates inputs or reads public formats (Delta log JSON, Parquet,
DynamoDB items); expected results live in the test modules.
"""

import base64
import datetime
import io
import json
import time
import uuid
from collections import Counter
from decimal import Decimal
from pathlib import Path
from urllib.parse import unquote

import boto3
import pyarrow as pa
import pyarrow.parquet as pq
from harness import run_command, start_command

UTC = datetime.timezone.utc
DLQ_FIELDS = [("base64_bytes", "string"), ("json_string", "string"), ("error", "string"), ("timestamp", "timestamp")]


def unique(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def ts(*args, us=0):
    """A UTC datetime for expected timestamp values."""
    return datetime.datetime(*args, microsecond=us, tzinfo=UTC)


def struct(*fields):
    return {"type": "struct", "fields": [field(n, t) for n, t in fields]}


def array(element):
    return {"type": "array", "elementType": element, "containsNull": True}


def field(name, kind, nullable=True):
    return {"name": name, "type": kind, "nullable": nullable, "metadata": {}}


def v0_commit(fields, partitions=()):
    schema = {"type": "struct", "fields": [f if isinstance(f, dict) else field(*f) for f in fields]}
    actions = [
        {"commitInfo": {"timestamp": 1700000000000, "operation": "CREATE TABLE", "operationParameters": {"isManaged": "false", "description": None, "partitionBy": "[]", "properties": "{}"}, "isBlindAppend": True}},
        {"protocol": {"minReaderVersion": 1, "minWriterVersion": 2}},
        {"metaData": {"id": str(uuid.uuid4()), "format": {"provider": "parquet", "options": {}}, "schemaString": json.dumps(schema), "partitionColumns": list(partitions), "createdTime": 1700000000000, "configuration": {}}},
    ]
    return "".join(json.dumps(a) + "\n" for a in actions)


# --- value comparison -------------------------------------------------------------
# Booleans stay distinct from numbers; numbers compare by value (1 == 1.0 == 1e0);
# timestamps compare as UTC instants with microsecond precision.


def norm(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, int):
        return ("num", Decimal(value))
    if isinstance(value, float):
        return ("num", Decimal(repr(value)))
    if isinstance(value, Decimal):
        return ("num", value)
    if isinstance(value, str):
        return ("str", value)
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return ("ts", round((value - datetime.datetime(1970, 1, 1, tzinfo=UTC)) / datetime.timedelta(microseconds=1)))
    if isinstance(value, datetime.date):
        return ("date", value.isoformat())
    if isinstance(value, bytes):
        return ("bin", value)
    if isinstance(value, (list, tuple)):
        return ("list", tuple(norm(v) for v in value))
    if isinstance(value, dict):
        return ("obj", tuple(sorted((k, norm(v)) for k, v in value.items())))
    raise TypeError(f"unexpected value {value!r}")


def assert_rows(actual, expected):
    """Multiset equality of rows under the value policy above."""
    got = Counter(norm(r) for r in actual)
    want = Counter(norm(r) for r in expected)
    assert got == want, f"rows differ\nmissing: {list((want - got).elements())}\nunexpected: {list((got - want).elements())}"


class JsonText:
    """Expected text of a JSON value stored in a string column; compared as a JSON value."""

    def __init__(self, text):
        self.text = text

    def __repr__(self):
        return f"JsonText({self.text!r})"


def same(actual, expected):
    if isinstance(expected, JsonText):
        return isinstance(actual, str) and json_value(actual) == json_value(expected.text)
    if isinstance(expected, dict):
        return isinstance(actual, dict) and set(actual) == set(expected) and all(same(actual[k], v) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(same(a, e) for a, e in zip(actual, expected))
    return norm(actual) == norm(expected)


def assert_rows_by(actual, expected, key):
    """Rows identified by a unique key column; each compared field by field."""
    got = {}
    for row in actual:
        assert row[key] not in got, f"duplicate row for {key}={row[key]!r}: {row}"
        got[row[key]] = row
    want = {row[key]: row for row in expected}
    assert sorted(got, key=repr) == sorted(want, key=repr), f"{key} values differ: got {sorted(got, key=repr)}, want {sorted(want, key=repr)}"
    for k, row in want.items():
        assert set(got[k]) == set(row), f"row {key}={k!r} columns {sorted(got[k])} != {sorted(row)}"
        for column, value in row.items():
            assert same(got[k].get(column), value), f"row {key}={k!r} column {column}: got {got[k].get(column)!r}, want {value!r}"


def json_value(text):
    """Parse a JSON text for comparison by value (key order and spacing ignored)."""
    return norm(json.loads(text, parse_float=Decimal, parse_int=Decimal))


def project(value, kind):
    """Shape a decoded Parquet value by the Delta type: absent struct fields read as null."""
    if value is None:
        return None
    if isinstance(kind, dict) and kind.get("type") == "struct":
        if not isinstance(value, dict):
            return value
        return {f["name"]: project(value.get(f["name"]), f["type"]) for f in kind["fields"]}
    if isinstance(kind, dict) and kind.get("type") == "array":
        if not isinstance(value, list):
            return value
        return [project(v, kind["elementType"]) for v in value]
    return value


def partition_value(text, kind):
    """Read a partitionValues string as a typed value (Delta partition value serialization)."""
    if text is None:
        return None
    if kind in ("integer", "long", "short", "byte"):
        return int(text)
    if kind in ("float", "double"):
        return float(text)
    if kind == "boolean":
        return {"true": True, "false": False}[text]
    if kind == "date":
        return datetime.date.fromisoformat(text)
    return text


PRIMITIVE_TYPES = {
    "string": lambda t: pa.types.is_string(t) or pa.types.is_large_string(t),
    "binary": lambda t: pa.types.is_binary(t) or pa.types.is_large_binary(t),
    "integer": pa.types.is_int32,
    "long": pa.types.is_int64,
    "short": pa.types.is_int16,
    "byte": pa.types.is_int8,
    "float": pa.types.is_float32,
    "double": pa.types.is_float64,
    "boolean": pa.types.is_boolean,
    # Any unit; zone either UTC or absent (both read as UTC instants).
    "timestamp": lambda t: pa.types.is_timestamp(t) and t.tz in (None, "UTC", "+00:00", "Etc/UTC"),
    "date": pa.types.is_date32,
}


def arrow_type_matches(arrow_type, kind):
    """Whether a data file's Arrow type is the Parquet representation of a Delta type."""
    if pa.types.is_dictionary(arrow_type):
        arrow_type = arrow_type.value_type
    if isinstance(kind, dict) and kind.get("type") == "struct":
        if not pa.types.is_struct(arrow_type):
            return False
        children = {arrow_type.field(i).name: arrow_type.field(i).type for i in range(arrow_type.num_fields)}
        return all(f["name"] not in children or arrow_type_matches(children[f["name"]], f["type"]) for f in kind["fields"])
    if isinstance(kind, dict) and kind.get("type") == "array":
        if not (pa.types.is_list(arrow_type) or pa.types.is_large_list(arrow_type)):
            return False
        return arrow_type_matches(arrow_type.value_type, kind["elementType"])
    return PRIMITIVE_TYPES[kind](arrow_type)


def check_file_types(arrow_schema, types):
    """Every schema column present in a data file has the type its Delta type maps to (absent columns read as null)."""
    for name, kind in types.items():
        index = arrow_schema.get_field_index(name)
        if index >= 0:
            arrow_type = arrow_schema.field(index).type
            assert arrow_type_matches(arrow_type, kind), f"column {name}: Parquet type {arrow_type} does not hold Delta type {json.dumps(kind)}"


def microsecond_type(arrow_type):
    """The same Arrow type with every timestamp (at any depth) in microseconds."""
    if pa.types.is_timestamp(arrow_type):
        return pa.timestamp("us", tz=arrow_type.tz)
    if pa.types.is_struct(arrow_type):
        return pa.struct([f.with_type(microsecond_type(f.type)) for f in arrow_type])
    if pa.types.is_list(arrow_type):
        return pa.list_(arrow_type.value_field.with_type(microsecond_type(arrow_type.value_type)))
    if pa.types.is_large_list(arrow_type):
        return pa.large_list(arrow_type.value_field.with_type(microsecond_type(arrow_type.value_type)))
    return arrow_type


def to_rows(table):
    """Decode a Parquet table; timestamps of finer units are truncated to the compared microsecond precision."""
    target = pa.schema([f.with_type(microsecond_type(f.type)) for f in table.schema])
    if target != table.schema:
        table = table.cast(target, safe=False)
    return table.to_pylist()


class Table:
    """A Delta table under s3://bucket/prefix, read independently of the program."""

    def __init__(self, bucket, prefix):
        self.bucket, self.prefix = bucket, prefix
        self.uri = f"s3://{bucket}/{prefix}"
        self.s3 = boto3.client("s3")

    def create(self, fields, partitions=()):
        self.put_log(0, v0_commit(fields, partitions))
        return self

    def put_log(self, version, text):
        self.s3.put_object(Bucket=self.bucket, Key=f"{self.prefix}/_delta_log/{version:020d}.json", Body=text.encode())

    def keys(self):
        out = {}
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=self.prefix + "/"):
            for o in page.get("Contents", []):
                out[o["Key"][len(self.prefix) + 1:]] = o["Size"]
        return out

    def read(self, relative):
        return self.s3.get_object(Bucket=self.bucket, Key=f"{self.prefix}/{relative}")["Body"].read()

    def versions(self):
        names = [k.split("/", 1)[1] for k in self.keys() if k.startswith("_delta_log/")]
        return sorted(int(n[:20]) for n in names if len(n) == 25 and n.endswith(".json") and n[:20].isdigit())

    def latest(self):
        v = self.versions()
        return v[-1] if v else -1

    def commit(self, version):
        text = self.read(f"_delta_log/{version:020d}.json").decode()
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def log(self):
        return {v: self.commit(v) for v in self.versions()}

    def metadata(self):
        meta = None
        for v in self.versions():
            for a in self.commit(v):
                if "metaData" in a:
                    meta = a["metaData"]
        if meta is None:  # earlier commit files removed: the latest checkpoint holds the table metadata
            checkpoints = sorted(k for k in self.keys() if k.endswith(".checkpoint.parquet"))
            rows = pq.read_table(io.BytesIO(self.read(checkpoints[-1]))).to_pylist()
            meta = next(r["metaData"] for r in rows if r.get("metaData"))
            meta = {**meta, "partitionColumns": list(meta["partitionColumns"])}
        return meta

    def schema(self):
        return json.loads(self.metadata()["schemaString"])

    def txns(self, upto=None):
        """Latest txn version per appId over commits <= upto."""
        out = {}
        for v in self.versions():
            if upto is not None and v > upto:
                break
            for a in self.commit(v):
                if "txn" in a:
                    out[a["txn"]["appId"]] = a["txn"]["version"]
        return out

    def file_rows(self, add):
        """Rows of one add action's file, shaped by the table schema, partition columns from partitionValues."""
        schema = self.schema()
        partitions = self.metadata()["partitionColumns"]
        types = {f["name"]: f["type"] for f in schema["fields"]}
        table = pq.read_table(io.BytesIO(self.read(unquote(add["path"]))))
        check_file_types(table.schema, types)
        rows = []
        for raw in to_rows(table):
            row = {name: project(raw.get(name), kind) for name, kind in types.items()}
            for column in partitions:
                row[column] = partition_value(add["partitionValues"][column], types[column])
            rows.append(row)
        return rows

    def adds(self, version):
        return [a["add"] for a in self.commit(version) if "add" in a]

    def rows(self, versions=None):
        versions = self.versions() if versions is None else versions
        out = []
        for v in versions:
            for add in self.adds(v):
                out += self.file_rows(add)
        return out

    def check_adds(self, version):
        """Each add of a commit names an existing data file of the recorded size and all partition columns."""
        keys = self.keys()
        partitions = self.metadata()["partitionColumns"]
        for add in self.adds(version):
            key = unquote(add["path"])
            assert key in keys, f"add path {add['path']} has no object"
            assert add["size"] == keys[key], f"add size {add['size']} != object size {keys[key]}"
            assert sorted(add["partitionValues"]) == sorted(partitions), add["partitionValues"]
            assert add["dataChange"] is True

    def wait_version(self, version, timeout=40):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.latest() >= version:
                return
            time.sleep(0.5)
        raise AssertionError(f"{self.uri}: version {version} not written within {timeout}s; have {self.versions()}")

    def delete_all(self):
        for key in self.keys():
            self.s3.delete_object(Bucket=self.bucket, Key=f"{self.prefix}/{key}")


def kinds(actions):
    return Counter(next(iter(a)) for a in actions)


def txn_actions(actions):
    return Counter((a["txn"]["appId"], a["txn"]["version"]) for a in actions if "txn" in a)


def b64(data):
    return base64.b64encode(data).decode()


class Run:
    """A program process started as the candidate; output goes to files in the workspace."""

    def __init__(self, args, cwd, env):
        self.cwd = Path(cwd)
        tag = uuid.uuid4().hex[:6]
        self.out_path, self.err_path = self.cwd / f"out-{tag}.txt", self.cwd / f"err-{tag}.txt"
        self.started = time.time()
        with open(self.out_path, "wb") as out, open(self.err_path, "wb") as err:
            self.proc = start_command(["/app/program", *map(str, args)], cwd=cwd, env=env, stdout=out, stderr=err)

    def poll(self):
        return self.proc.poll()

    def wait(self, timeout):
        try:
            return self.proc.wait(timeout=timeout)
        except Exception:
            self.stop()
            raise AssertionError(f"program did not exit within {timeout}s\n{self.output()}")

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except Exception:
                self.proc.kill()
                self.proc.wait()
        self.ended = time.time()

    def output(self):
        def tail(path):
            try:
                return path.read_bytes()[-3000:].decode(errors="replace")
            except OSError:
                return ""
        return f"stdout:\n{tail(self.out_path)}\nstderr:\n{tail(self.err_path)}"


class Lake:
    """Per-test resources: one bucket, its topics, program runs; all removed in teardown."""

    def __init__(self, brokers, lock_table, cwd):
        self.brokers, self.lock_table, self.cwd = brokers, lock_table, cwd
        self.bucket = unique("lake")
        self.s3 = boto3.client("s3")
        self.s3.create_bucket(Bucket=self.bucket)
        self.topics, self.runs = [], []
        self.env = {"AWS_ALLOW_HTTP": "true", "AWS_S3_LOCKING_PROVIDER": "dynamodb", "DELTA_DYNAMO_TABLE_NAME": lock_table}

    # Kafka
    def topic(self, partitions=1):
        from confluent_kafka.admin import AdminClient, NewTopic

        name = unique("topic")
        admin = AdminClient({"bootstrap.servers": self.brokers})
        deadline = time.time() + 60
        while True:
            try:
                admin.create_topics([NewTopic(name, num_partitions=partitions, replication_factor=1)])[name].result(20)
                break
            except Exception as error:  # the broker may still be settling after cluster creation
                if "TOPIC_ALREADY_EXISTS" in str(error):
                    break
                if time.time() > deadline:
                    raise
                time.sleep(2)
        self.topics.append(name)
        self.wait_partitions(name, partitions)
        return name

    def wait_partitions(self, name, partitions):
        from confluent_kafka.admin import AdminClient

        admin = AdminClient({"bootstrap.servers": self.brokers})
        deadline = time.time() + 30
        while time.time() < deadline:
            meta = admin.list_topics(name, timeout=10).topics.get(name)
            if meta is not None and meta.error is None and len(meta.partitions) == partitions and all(p.leader >= 0 for p in meta.partitions.values()):
                return
            time.sleep(0.5)

    def produce(self, topic, messages):
        """messages: (partition, payload[, timestamp_ms]); dict payloads are JSON-encoded, bytes raw, None a null value.

        Returns the (partition, offset) of each message in order."""
        from confluent_kafka import Producer

        producer = Producer({"bootstrap.servers": self.brokers, "message.timeout.ms": 20000, "enable.idempotence": False})
        delivered = []

        def report(error, msg):
            if error is not None:
                raise RuntimeError(f"produce failed: {error}")
            delivered.append((msg.partition(), msg.offset()))

        for item in messages:
            partition, payload = item[0], item[1]
            if isinstance(payload, (dict, list)):
                payload = json.dumps(payload).encode()
            elif isinstance(payload, str):
                payload = payload.encode()
            options = {"timestamp": item[2]} if len(item) > 2 else {}
            producer.produce(topic, value=payload, partition=partition, on_delivery=report, **options)
            assert producer.flush(20) == 0, "message not delivered"
        assert len(delivered) == len(messages)
        return delivered

    # Delta
    def table(self, fields, partitions=(), name=None):
        return Table(self.bucket, name or unique("tbl")).create(fields, partitions)

    def bare(self, name=None):
        return Table(self.bucket, name or unique("tbl"))

    # Program
    def args(self, topic, table, *extra, app=None, group=None, latency=2):
        return ["ingest", topic, table.uri, "--kafka", self.brokers, "--app_id", app or unique("app"),
                "--consumer_group", group or unique("group"), "--allowed_latency", latency, *extra]

    def start(self, args, env=None):
        run = Run(args, self.cwd, {**self.env, **(env or {})})
        self.runs.append(run)
        return run

    def run(self, args, timeout=40, env=None):
        """Run to completion (for invocations expected to end by themselves)."""
        return run_command(["/app/program", *map(str, args)], cwd=self.cwd, env={**self.env, **(env or {})},
                           capture_output=True, text=True, timeout=timeout)

    def lock_items(self, table):
        ddb = boto3.client("dynamodb")
        items, start = [], None
        while True:
            kwargs = {"TableName": self.lock_table, "KeyConditionExpression": "tablePath = :p",
                      "ExpressionAttributeValues": {":p": {"S": table.uri}}, "ConsistentRead": True}
            if start:
                kwargs["ExclusiveStartKey"] = start
            page = ddb.query(**kwargs)
            items += page["Items"]
            start = page.get("LastEvaluatedKey")
            if not start:
                return items

    def close(self):
        for run in self.runs:
            run.stop()
        try:
            for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket):
                for o in page.get("Contents", []):
                    self.s3.delete_object(Bucket=self.bucket, Key=o["Key"])
            self.s3.delete_bucket(Bucket=self.bucket)
        except Exception:
            pass
        if self.topics:
            try:
                from confluent_kafka.admin import AdminClient

                AdminClient({"bootstrap.servers": self.brokers}).delete_topics(self.topics, operation_timeout=10)
            except Exception:
                pass


def quiet(table, version, seconds):
    """Wait, then assert that the log still ends at `version`."""
    time.sleep(seconds)
    assert table.versions()[-1] == version, f"unexpected later commits: {table.versions()}"


def check_lock_items(lake, table, versions, started, ended):
    """One complete DynamoDB log-store item per commit the program wrote, none for others."""
    items = lake.lock_items(table)
    names = sorted(i["fileName"]["S"] for i in items)
    assert names == [f"{v:020d}.json" for v in sorted(versions)], names
    for item in items:
        assert item["tablePath"] == {"S": table.uri}
        assert item["complete"] == {"S": "true"}, item
        assert set(item["tempPath"]) == {"S"} and item["tempPath"]["S"], item
        expire = int(item["expireTime"]["N"])
        assert started + 86400 - 120 <= expire <= ended + 86400 + 120, (expire, started, ended)
