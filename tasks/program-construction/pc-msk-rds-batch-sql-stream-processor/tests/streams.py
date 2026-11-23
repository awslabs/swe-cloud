"""Kafka/Postgres fixture plumbing and comparison helpers shared by the suite.

Nothing here computes the program's answers: it creates inputs, reads back
what the program left in Kafka and Postgres, and compares decoded JSON under
the public value policy.
"""

import json
import signal
import subprocess
import time
import uuid
from collections import Counter
from decimal import Decimal

import psycopg
from confluent_kafka import OFFSET_BEGINNING, Consumer, Producer, TopicPartition
from confluent_kafka.admin import AdminClient, NewTopic

from harness import run_program, start_command, write_fixture

QUIET = {"SQLFLOW_LOG_LEVEL": "WARN"}


# ----------------------------------------------------------------- comparison
def canon(value):
    """A hashable form of decoded JSON: numbers by value, never equal to booleans."""
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, (int, float, Decimal)):
        return ("num", Decimal(str(value)) if isinstance(value, float) else Decimal(value))
    if isinstance(value, str):
        return ("str", value)
    if value is None:
        return ("null",)
    if isinstance(value, list):
        return ("list", tuple(canon(v) for v in value))
    if isinstance(value, dict):
        return ("obj", tuple(sorted((k, canon(v)) for k, v in value.items())))
    raise TypeError(f"not decoded JSON: {value!r}")


def decode(raw):
    if isinstance(raw, bytes):
        raw = raw.decode()
    return json.loads(raw, parse_float=Decimal)


def multiset(objects):
    return Counter(canon(o) for o in objects)


def same(observed, expected):
    """Decoded JSON equality under the value policy (order of object keys is free)."""
    return canon(observed) == canon(expected)


def assert_records(observed, expected):
    """Unordered comparison that keeps multiplicity."""
    got, want = multiset(observed), multiset(expected)
    assert got == want, f"records differ\nobserved: {observed}\nexpected: {expected}"


def assert_distinct(observed, expected):
    """Window output: the set of distinct records, ignoring republication."""
    got, want = set(multiset(observed)), set(multiset(expected))
    assert got == want, f"distinct records differ\nobserved: {observed}\nexpected: {expected}"


# ----------------------------------------------------------------------- kafka
class Kafka:
    def __init__(self, brokers):
        self.brokers = brokers
        self.admin = AdminClient({"bootstrap.servers": brokers})

    def create_topics(self, names, partitions=1):
        futures = self.admin.create_topics([NewTopic(n, partitions, 1) for n in names], operation_timeout=30)
        for future in futures.values():
            future.result(timeout=30)

    def delete_topics(self, names):
        if not names:
            return
        futures = self.admin.delete_topics(list(names), operation_timeout=30)
        for future in futures.values():
            try:
                future.result(timeout=30)
            except Exception:  # noqa: BLE001 -- cleanup is best effort
                pass

    def produce(self, topic, values, *, partition=None, keys=None, timestamps=None):
        """Produce raw values (str/bytes, or dicts encoded as JSON) in order."""
        producer = Producer({"bootstrap.servers": self.brokers, "enable.idempotence": False, "linger.ms": 0})
        for i, value in enumerate(values):
            if isinstance(value, (dict, list)):
                value = json.dumps(value)
            if isinstance(value, str):
                value = value.encode()
            options = {}
            if partition is not None:
                options["partition"] = partition
            if keys is not None:
                options["key"] = keys[i]
            if timestamps is not None:
                options["timestamp"] = timestamps[i]
            producer.produce(topic, value, **options)
            producer.poll(0)
        assert producer.flush(30) == 0, "fixture records were not delivered"

    def read(self, topic, partitions=1):
        """Every record of the topic, as dicts with partition/offset/key/value bytes."""
        consumer = Consumer({"bootstrap.servers": self.brokers, "group.id": "verifier-reader-" + uuid.uuid4().hex,
                             "enable.auto.commit": False})
        try:
            ends = {}
            for p in range(partitions):
                low, high = consumer.get_watermark_offsets(TopicPartition(topic, p), timeout=15)
                ends[p] = high
            pending = {p for p, high in ends.items() if high > 0}
            if not pending:
                return []
            consumer.assign([TopicPartition(topic, p, OFFSET_BEGINNING) for p in pending])
            records = []
            deadline = time.monotonic() + 20
            while pending and time.monotonic() < deadline:
                message = consumer.poll(0.5)
                if message is None or message.error():
                    continue
                records.append({"partition": message.partition(), "offset": message.offset(),
                                "key": message.key(), "value": message.value()})
                if message.offset() + 1 >= ends[message.partition()]:
                    pending.discard(message.partition())
            assert not pending, f"could not read {topic} to its end"
            records.sort(key=lambda r: (r["partition"], r["offset"]))
            return records
        finally:
            consumer.close()

    def values(self, topic, partitions=1):
        return [decode(r["value"]) for r in self.read(topic, partitions)]

    def committed(self, group, topic, partitions=1):
        """The group's committed offset per partition; None where nothing is committed."""
        consumer = Consumer({"bootstrap.servers": self.brokers, "group.id": group, "enable.auto.commit": False})
        try:
            result = consumer.committed([TopicPartition(topic, p) for p in range(partitions)], timeout=15)
            return [tp.offset if tp.offset >= 0 else None for tp in result]
        finally:
            consumer.close()

    def commit(self, group, topic, offsets):
        """Commit offsets for a group as an operator would, before the program runs."""
        consumer = Consumer({"bootstrap.servers": self.brokers, "group.id": group, "enable.auto.commit": False})
        try:
            consumer.commit(offsets=[TopicPartition(topic, p, o) for p, o in enumerate(offsets)], asynchronous=False)
        finally:
            consumer.close()


# --------------------------------------------------------------------- program
class Stream:
    """One test's isolated names, workspace and cleanup inside the shared engines."""

    def __init__(self, venue, root):
        self.venue = venue
        self.kafka = venue.kafka
        self.root = root
        self.prefix = "t" + uuid.uuid4().hex[:10]
        self.group = self.prefix + "-group"
        self.topics = []
        self.tables = []
        self.processes = []

    # names
    def topic(self, name, partitions=1):
        full = f"{self.prefix}-{name}"
        self.kafka.create_topics([full], partitions)
        self.topics.append(full)
        return full

    def table(self, name, columns):
        full = f"{self.prefix}_{name}"
        self.sql(f'CREATE TABLE "{full}" ({columns})')
        self.tables.append(full)
        return full

    # postgres
    def sql(self, statement, params=None):
        with psycopg.connect(self.venue.dsn, autocommit=True) as conn:
            cursor = conn.execute(statement, params)
            return cursor.fetchall() if cursor.description else None

    # program
    def env(self, extra=None):
        return {**QUIET, "SQLFLOW_KAFKA_BROKERS": self.venue.brokers, "SQLFLOW_PG_DSN": self.venue.dsn,
                "SQLFLOW_GROUP_ID": self.group, **(extra or {})}

    def config(self, text, name="pipeline.yml"):
        write_fixture(self.root / name, text)
        return name

    def run(self, args, env=None, timeout=45):
        return run_program(args, cwd=self.root, env=self.env(env), timeout=timeout)

    def start(self, args, env=None):
        """Start a long-running `run`; stop() sends SIGTERM and waits."""
        process = start_command(["/app/program", *map(str, args)], cwd=self.root, env=self.env(env),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.processes.append(process)
        return process

    def stop(self, process, sig=signal.SIGTERM, timeout=40):
        process.send_signal(sig)
        try:
            out, err = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            out, err = process.communicate()
            raise AssertionError("the program did not stop after the signal") from None
        return subprocess.CompletedProcess(process.args, process.returncode, out, err)

    def stats(self, name="stats.json"):
        path = self.root / name
        assert path.is_file(), "the run wrote no stats file"
        return decode(path.read_bytes())

    def cleanup(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
                process.wait()
        self.kafka.delete_topics(self.topics)
        for table in self.tables:
            try:
                self.sql(f'DROP TABLE IF EXISTS "{table}"')
            except psycopg.Error:
                pass


def wait_until(predicate, timeout, interval=0.5):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value or time.monotonic() >= deadline:
            return value
        time.sleep(interval)


def source_block(topics="{{ SQLFLOW_INPUT_TOPIC }}", reset="earliest", event_time=None):
    lines = [
        "  source:",
        "    type: kafka",
        "    kafka:",
        "      brokers: [{{ SQLFLOW_KAFKA_BROKERS }}]",
        "      group_id: {{ SQLFLOW_GROUP_ID }}",
        f"      auto_offset_reset: {reset}",
        f"      topics: [{topics}]",
    ]
    if event_time:
        lines.append(f"      event_time: {event_time}")
    return "\n".join(lines) + "\n"


def kafka_sink(topic="{{ SQLFLOW_OUTPUT_TOPIC }}", key=None, indent="  ", name="sink"):
    text = (f"{indent}{name}:\n{indent}  type: kafka\n{indent}  kafka:\n"
            f"{indent}    brokers: [{{{{ SQLFLOW_KAFKA_BROKERS }}}}]\n{indent}    topic: {topic}\n")
    if key:
        text += f"{indent}    key: {key}\n"
    return text


def postgres_sink(table, mode, key=None, indent="  ", name="sink"):
    text = (f"{indent}{name}:\n{indent}  type: postgres\n{indent}  postgres:\n"
            f'{indent}    dsn: "{{{{ SQLFLOW_PG_DSN }}}}"\n{indent}    table: {table}\n{indent}    mode: {mode}\n')
    if key:
        text += f"{indent}    key: [{', '.join(key)}]\n"
    return text


def pipeline(sql, sink, *, batch_size=10, handler="handlers.InferredMemBatch", extra="", source=None, tables=""):
    """A pipeline config; sql is indented as a block scalar."""
    body = "\n".join("      " + line for line in sql.strip().splitlines())
    return (f"{tables}pipeline:\n  batch_size: {batch_size}\n{extra}{source or source_block()}"
            f"  handler:\n    type: {handler}\n    sql: |\n{body}\n{sink}")


def dlq_block(policy, topic="{{ SQLFLOW_DLQ_TOPIC }}"):
    text = f"  on_error:\n    policy: {policy}\n"
    if policy == "DLQ":
        text += kafka_sink(topic, indent="    ", name="dlq")
    return text


def window_tables(*, size=3600, grace=0, idle=0, lateness=0, emit, sink, columns="bucket TIMESTAMPTZ, city VARCHAR, count BIGINT"):
    emit_body = "\n".join("          " + line for line in emit.strip().splitlines())
    return (
        "tables:\n  sql:\n    - name: agg\n"
        f"      sql: CREATE TABLE agg ({columns})\n"
        "      window:\n        time_column: bucket\n"
        f"        size_seconds: {size}\n        grace_seconds: {grace}\n"
        f"        idle_close_seconds: {idle}\n        allowed_lateness_seconds: {lateness}\n"
        f"        emit_sql: |\n{emit_body}\n{sink}"
    )

