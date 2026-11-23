"""Session MSK cluster, per-test topics/groups and small read-back helpers."""

import os
import tempfile
import time
import uuid

import boto3
import pytest
from confluent_kafka import OFFSET_BEGINNING, Consumer, ConsumerGroupTopicPartitions, Producer, TopicPartition
from confluent_kafka.admin import AdminClient, NewTopic
from harness import run_program


def pytest_collection_modifyitems(items):
    # The session cluster is created inside whichever test runs first.
    if items:
        items[0].add_marker(pytest.mark.timeout(600))


@pytest.fixture(scope="session")
def brokers():
    kafka = boto3.client("kafka")
    arn = kafka.create_cluster(
        ClusterName="spt-" + uuid.uuid4().hex[:12],
        KafkaVersion="3.6.0",
        NumberOfBrokerNodes=1,
        BrokerNodeGroupInfo={"InstanceType": "kafka.t3.small", "ClientSubnets": ["subnet-a", "subnet-b"]},
    )["ClusterArn"]
    try:
        deadline = time.time() + 540
        while True:
            state = kafka.describe_cluster(ClusterArn=arn)["ClusterInfo"]["State"]
            if state == "ACTIVE":
                break
            assert state != "FAILED" and time.time() < deadline, f"cluster state {state}"
            time.sleep(2)
        bootstrap = kafka.get_bootstrap_brokers(ClusterArn=arn)["BootstrapBrokerString"]
        admin = AdminClient({"bootstrap.servers": bootstrap})
        while True:
            try:
                admin.list_topics(timeout=5)
                break
            except Exception:
                assert time.time() < deadline, "brokers never answered metadata requests"
                time.sleep(1)
        yield bootstrap
    finally:
        kafka.delete_cluster(ClusterArn=arn)


@pytest.fixture(scope="session")
def clean_home():
    """An empty, read-only HOME so no default configuration file is found."""
    path = tempfile.mkdtemp(prefix="spt-home-")
    os.chmod(path, 0o755)
    yield path
    os.rmdir(path)


class Kafka:
    def __init__(self, bootstrap, home):
        self.bootstrap = bootstrap
        self.home = home
        self.admin = AdminClient({"bootstrap.servers": bootstrap})
        self.topics = []
        self.groups = []

    def name(self, prefix):
        return f"{prefix}-{uuid.uuid4().hex[:10]}"

    def group(self):
        group = self.name("grp")
        self.groups.append(group)
        return group

    def topic(self, partitions=1, config=None):
        topic = self.name("t")
        futures = self.admin.create_topics([NewTopic(topic, partitions, 1, config=config or {})])
        futures[topic].result()
        self.topics.append(topic)
        deadline = time.time() + 30
        while True:
            md = self.admin.list_topics(timeout=5).topics.get(topic)
            if md and len(md.partitions) == partitions and all(p.leader >= 0 for p in md.partitions.values()):
                return topic
            assert time.time() < deadline, f"topic {topic} never became ready"
            time.sleep(0.3)

    def run(self, *args, stdin=None, timeout=30, env=None, cwd=None):
        """Invoke the program with -b and a clean HOME; output stays bytes."""
        return run_program(["-b", self.bootstrap, *args], stdin=stdin, timeout=timeout, cwd=cwd, env={"HOME": self.home, **(env or {})}, text=False)

    def produce(self, topic, records):
        """records: dicts with partition, value and optional key, timestamp, headers."""
        producer = Producer({"bootstrap.servers": self.bootstrap})
        for r in records:
            producer.produce(topic, value=r.get("value"), key=r.get("key"), partition=r["partition"], timestamp=r.get("timestamp", 0), headers=r.get("headers"))
        assert producer.flush(15) == 0

    def records(self, topic, isolation="read_uncommitted"):
        """All records of every partition: {partition: [record dict in offset order]}."""
        consumer = Consumer({
            "bootstrap.servers": self.bootstrap,
            "group.id": "readback-" + uuid.uuid4().hex,
            "enable.auto.commit": False,
            "enable.partition.eof": True,
            "isolation.level": isolation,
        })
        try:
            partitions = sorted(consumer.list_topics(topic, timeout=10).topics[topic].partitions)
            consumer.assign([TopicPartition(topic, p, OFFSET_BEGINNING) for p in partitions])
            result = {p: [] for p in partitions}
            at_eof = set()
            deadline = time.time() + 20
            while len(at_eof) < len(partitions):
                assert time.time() < deadline, "read-back did not reach the end of every partition"
                m = consumer.poll(0.5)
                if m is None:
                    continue
                if m.error():
                    assert m.error().code() == -191, str(m.error())  # _PARTITION_EOF
                    at_eof.add(m.partition())
                    continue
                at_eof.discard(m.partition())
                ts_type, ts = m.timestamp()
                result[m.partition()].append({
                    "offset": m.offset(),
                    "key": m.key(),
                    "value": m.value(),
                    "headers": list(m.headers() or []),
                    "ts_type": ts_type,
                    "ts": ts,
                })
            return result
        finally:
            consumer.close()

    def watermarks(self, topic, partition):
        consumer = Consumer({"bootstrap.servers": self.bootstrap, "group.id": "wm-" + uuid.uuid4().hex})
        try:
            return consumer.get_watermark_offsets(TopicPartition(topic, partition), timeout=10)
        finally:
            consumer.close()

    def committed(self, group, topic_partitions):
        """{(topic, partition): committed offset or None} for the listed (topic, partition-count) pairs."""
        tps = [TopicPartition(t, p) for t, n in topic_partitions for p in range(n)]
        request = ConsumerGroupTopicPartitions(group, tps)
        result = self.admin.list_consumer_group_offsets([request])[group].result()
        return {(tp.topic, tp.partition): (tp.offset if tp.offset >= 0 else None) for tp in result.topic_partitions}

    def commit(self, group, topic, offsets):
        consumer = Consumer({"bootstrap.servers": self.bootstrap, "group.id": group, "enable.auto.commit": False})
        try:
            consumer.commit(offsets=[TopicPartition(topic, p, o) for p, o in offsets.items()], asynchronous=False)
        finally:
            consumer.close()

    def metadata(self):
        return self.admin.list_topics(timeout=10)

    def cleanup(self):
        if self.groups:
            for future in self.admin.delete_consumer_groups(self.groups).values():
                try:
                    future.result()
                except Exception:
                    pass
        if self.topics:
            for future in self.admin.delete_topics(self.topics).values():
                try:
                    future.result()
                except Exception:
                    pass


@pytest.fixture
def kafka(brokers, clean_home):
    k = Kafka(brokers, clean_home)
    try:
        yield k
    finally:
        k.cleanup()

