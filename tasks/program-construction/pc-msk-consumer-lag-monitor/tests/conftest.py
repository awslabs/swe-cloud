import time

import boto3
import pytest
from confluent_kafka import TopicPartition

from harness import workspace
from lagsvc import Kafka, Service, default_sections, free_port, unique


def pytest_collection_modifyitems(items):
    """The first test to request the MSK cluster pays for creating it."""
    for item in items:
        if "kafka" in getattr(item, "fixturenames", ()):
            if item.get_closest_marker("timeout") is None:
                item.add_marker(pytest.mark.timeout(600))
            break


def _warm_offsets_topic(kafka):
    """The broker creates its offsets topic lazily; the service needs it led before it starts."""
    topic = kafka.create_topic(unique("warmup"), 1)
    consumer_config = {"bootstrap.servers": kafka.bootstrap, "group.id": unique("warmup"), "enable.auto.commit": False}
    from confluent_kafka import Consumer

    consumer = Consumer(consumer_config)
    deadline = time.time() + 120
    try:
        while True:
            try:
                consumer.commit(offsets=[TopicPartition(topic, 0, 0)], asynchronous=False)
                break
            except Exception:
                if time.time() > deadline:
                    raise
                time.sleep(1)
    finally:
        consumer.close()
    while True:
        metadata = kafka.admin.list_topics("__consumer_offsets", timeout=10).topics.get("__consumer_offsets")
        if metadata is not None and metadata.error is None and metadata.partitions and all(p.leader >= 0 for p in metadata.partitions.values()):
            return
        if time.time() > deadline:
            raise AssertionError("offsets topic never became available")
        time.sleep(0.5)


@pytest.fixture(scope="session")
def kafka():
    client = boto3.client("kafka")
    arn = client.create_cluster(
        ClusterName=unique("lag"),
        KafkaVersion="3.6.0",
        NumberOfBrokerNodes=1,
        BrokerNodeGroupInfo={"InstanceType": "kafka.m5.large", "ClientSubnets": ["subnet-a", "subnet-b"]},
    )["ClusterArn"]
    try:
        deadline = time.time() + 500
        while client.describe_cluster(ClusterArn=arn)["ClusterInfo"]["State"] != "ACTIVE":
            assert time.time() < deadline, "MSK cluster never became ACTIVE"
            time.sleep(2)
        bootstrap = client.get_bootstrap_brokers(ClusterArn=arn)["BootstrapBrokerString"]
        broker = Kafka(bootstrap)
        _warm_offsets_topic(broker)
        yield broker
    finally:
        client.delete_cluster(ClusterArn=arn)


@pytest.fixture
def topics(kafka):
    """Factory for fresh topics, deleted after the test."""
    created = []

    def create(partitions, prefix="topic"):
        name = kafka.create_topic(unique(prefix), partitions)
        created.append(name)
        return name

    yield create
    for name in created:
        try:
            kafka.delete_topic(name)
        except Exception:
            pass


@pytest.fixture
def scratch():
    with workspace() as root:
        yield root


@pytest.fixture
def service(kafka):
    """Factory: service(cluster={...}, storage={...}, ...) starts a READY instance in a fresh workspace."""
    started = []
    contexts = []

    def start(wait=True, env=None, **overrides):
        context = workspace()
        root = context.__enter__()
        contexts.append(context)
        port = free_port()
        instance = Service(root, default_sections(kafka, port, root, **overrides), port, env=env)
        started.append(instance)
        return instance.wait_ready() if wait else instance

    yield start
    for instance in started:
        instance.close()
    for context in reversed(contexts):
        context.__exit__(None, None, None)
