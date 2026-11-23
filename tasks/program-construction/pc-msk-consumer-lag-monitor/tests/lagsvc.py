"""Shared helpers: Kafka fixtures on the MSK cluster and a running instance of /app/program."""

import math
import signal
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import requests
from confluent_kafka import Consumer, KafkaException, Producer, TopicPartition
from confluent_kafka.admin import AdminClient, NewTopic

from harness import run_command, start_command, write_fixture

CLUSTER = "msk"  # cluster/consumer module name used in every generated configuration


def unique(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def retry(action, attempts=20, delay=0.5):
    """Group-coordinator calls on a fresh broker occasionally fail transiently."""
    for attempt in range(attempts):
        try:
            return action()
        except KafkaException:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)
    return None


class Kafka:
    def __init__(self, bootstrap):
        self.bootstrap = bootstrap
        self.servers = bootstrap.split(",")
        self.admin = AdminClient({"bootstrap.servers": bootstrap})

    def create_topic(self, name, partitions):
        for future in self.admin.create_topics([NewTopic(name, partitions, 1)]).values():
            future.result(30)
        deadline = time.time() + 30
        while True:
            metadata = self.admin.list_topics(name, timeout=10).topics.get(name)
            if metadata is not None and metadata.error is None and len(metadata.partitions) == partitions and all(p.leader >= 0 for p in metadata.partitions.values()):
                return name
            if time.time() > deadline:
                raise AssertionError(f"topic {name} not ready")
            time.sleep(0.3)

    def delete_topic(self, name):
        for future in self.admin.delete_topics([name]).values():
            future.result(30)

    def produce(self, topic, counts):
        """Append counts[partition] records to each partition."""
        producer = Producer({"bootstrap.servers": self.bootstrap, "linger.ms": 0})
        for partition, count in counts.items():
            for index in range(count):
                producer.produce(topic, key=b"k%d" % index, value=b"record-%d" % index, partition=partition)
        assert producer.flush(30) == 0

    def end_offsets(self, topic, partitions):
        consumer = Consumer({"bootstrap.servers": self.bootstrap, "group.id": unique("watermark")})
        try:
            return [consumer.get_watermark_offsets(TopicPartition(topic, p), timeout=10)[1] for p in range(partitions)]
        finally:
            consumer.close()

    def commit(self, group, topic, offsets, metadata=None, leader_epoch=None):
        """Commit explicit offsets {partition: offset} for a group without joining it."""
        consumer = Consumer({"bootstrap.servers": self.bootstrap, "group.id": group, "enable.auto.commit": False})
        try:
            partitions = []
            for partition, offset in offsets.items():
                if metadata is not None or leader_epoch is not None:
                    partitions.append(TopicPartition(topic, partition, offset, metadata or "", -1 if leader_epoch is None else leader_epoch))
                else:
                    partitions.append(TopicPartition(topic, partition, offset))
            result = retry(lambda: consumer.commit(offsets=partitions, asynchronous=False))
            assert all(item.error is None for item in result), result
        finally:
            consumer.close()
        return int(time.time() * 1000)

    def delete_group(self, group):
        def delete():
            for future in self.admin.delete_consumer_groups([group]).values():
                future.result(20)
        retry(delete)

    def describe_group(self, group):
        return retry(lambda: self.admin.describe_consumer_groups([group])[group].result(20))


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, list | tuple):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_config(sections):
    lines = []
    for section, values in sections.items():
        lines.append(f"[{section}]")
        lines.extend(f"{key} = {toml_value(value)}" for key, value in values.items() if value is not None)
        lines.append("")
    return "\n".join(lines)


def default_sections(kafka, port, root, *, cluster=None, consumer=None, storage=None, evaluator=None, general=None):
    """A configuration with fast refreshes; keyword dicts override or extend each module section."""
    return {
        "general": {"pidfile": str(root / "burrow.pid"), **(general or {})},
        "logging": {"level": "info"},
        "client-profile.lag": {"client-id": "lag-checker", "kafka-version": "2.8.0"},
        f"cluster.{CLUSTER}": {
            "class-name": "kafka",
            "servers": kafka.servers,
            "client-profile": "lag",
            "topic-refresh": 1,
            "offset-refresh": 1,
            **(cluster or {}),
        },
        f"consumer.{CLUSTER}": {
            "class-name": "kafka",
            "cluster": CLUSTER,
            "servers": kafka.servers,
            "client-profile": "lag",
            **(consumer or {}),
        },
        "storage.default": {"class-name": "inmemory", "intervals": 5, "min-distance": 0, **(storage or {})},
        "evaluator.default": {"class-name": "caching", "expire-cache": 1, **(evaluator or {})},
        "httpserver.default": {"address": f"127.0.0.1:{port}"},
    }


class Service:
    """One running /app/program; stdout/stderr go to root-private anonymous files."""

    def __init__(self, root, sections, port, env=None):
        self.root = Path(root)
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        self.conf = self.root / "conf"
        write_fixture(self.conf / "burrow.toml", render_config(sections), mode=0o644)
        self.stdout = tempfile.TemporaryFile()
        self.stderr = tempfile.TemporaryFile()
        self.process = start_command(["/app/program", "--config-dir", str(self.conf)], cwd=str(self.root), stdout=self.stdout, stderr=self.stderr, env=env)

    def wait_ready(self, timeout=40):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise AssertionError(f"service exited early with {self.process.returncode}: {self.read_stderr()[-2000:]}")
            try:
                response = requests.get(self.base + "/burrow/admin/ready", timeout=2)
                if response.status_code == 200 and response.text == "READY":
                    return self
            except requests.RequestException:
                pass
            time.sleep(0.25)
        raise AssertionError("service never became READY")

    def read_stderr(self):
        self.stderr.seek(0)
        return self.stderr.read().decode(errors="replace")

    def request(self, method, path, **options):
        return requests.request(method, self.base + path, timeout=10, **options)

    def get(self, path):
        return self.request("GET", path)

    def json(self, path, status=200):
        response = self.get(path)
        assert response.status_code == status, (path, response.status_code, response.text)
        assert response.headers.get("Content-Type", "").startswith("application/json"), response.headers
        return response.json()

    def wait_json(self, path, predicate, timeout=25, status=200, interval=0.25):
        """Poll until predicate(body) holds; returns that body."""
        deadline = time.time() + timeout
        body = None
        while time.time() < deadline:
            response = self.get(path)
            if response.status_code == status:
                body = response.json()
                if predicate(body):
                    return body
            time.sleep(interval)
        raise AssertionError(f"timed out waiting on {path}; last body: {body}")

    def wait_topic(self, topic, offsets, timeout=25):
        """Wait until the service reports exactly these broker end offsets for topic."""
        return self.wait_json(f"/v3/kafka/{CLUSTER}/topic/{topic}", lambda body: body.get("offsets") == offsets, timeout)

    def detail(self, group):
        return self.json(f"/v3/kafka/{CLUSTER}/consumer/{group}")

    def wait_commit(self, group, topic, partition, offset, timeout=20):
        """Wait until the newest stored commit for the partition has this offset."""

        def newest(body):
            partitions = body.get("topics", {}).get(topic, [])
            if len(partitions) <= partition or not partitions[partition]["offsets"]:
                return False
            last = partitions[partition]["offsets"][-1]
            return last is not None and last["offset"] == offset

        return self.wait_json(f"/v3/kafka/{CLUSTER}/consumer/{group}", newest, timeout)

    def window(self, group, topic, partition):
        """The stored commits (non-empty slots) of one partition, oldest first; [] if unknown."""
        response = self.get(f"/v3/kafka/{CLUSTER}/consumer/{group}")
        if response.status_code != 200:
            return []
        partitions = response.json()["topics"].get(topic, [])
        if len(partitions) <= partition:
            return []
        return [entry for entry in partitions[partition]["offsets"] if entry is not None]

    def wait_window(self, group, topic, partition, offsets, timeout=20):
        """Wait until the stored commit offsets of a partition are exactly this list."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            window = self.window(group, topic, partition)
            if [entry["offset"] for entry in window] == offsets:
                return window
            time.sleep(0.2)
        raise AssertionError(f"{group} {topic}[{partition}] window never became {offsets}: {self.window(group, topic, partition)}")

    def lag(self, group, status=200):
        return self.json(f"/v3/kafka/{CLUSTER}/consumer/{group}/lag", status)

    def status(self, group, status=200):
        return self.json(f"/v3/kafka/{CLUSTER}/consumer/{group}/status", status)

    def consumers(self):
        return self.json(f"/v3/kafka/{CLUSTER}/consumer")["consumers"]

    def read_candidate_file(self, name):
        result = run_command(["cat", str(self.root / name)], capture_output=True, text=True, timeout=10)
        return result.returncode, result.stdout

    def stop(self, sig=signal.SIGTERM, timeout=20):
        if self.process.poll() is None:
            self.process.send_signal(sig)
            try:
                self.process.wait(timeout)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        return self.process.returncode

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait()
        self.stdout.close()
        self.stderr.close()


def by_partition(evaluation):
    """Partition statuses keyed by (topic, partition); order of the list is not significant."""
    keyed = {}
    for item in evaluation["partitions"]:
        key = (item["topic"], item["partition"])
        assert key not in keyed, f"duplicate partition entry {key}"
        keyed[key] = item
    return keyed


def is_int(value):
    return type(value) is int or (type(value) is float and value.is_integer())


def number(value, expected, tolerance=1e-6):
    """Numeric value equivalence (1 == 1.0 == 1e0), excluding booleans."""
    return type(value) in (int, float) and math.isclose(value, expected, abs_tol=tolerance)
