import time
import uuid

import boto3
import pytest
import redis

from harness import workspace
from relaykit import Relay, Store, config_text, free_port, unique

ENGINE_TIMEOUT = 600


def pytest_collection_modifyitems(items):
    """The session cache is provisioned by whichever selected test needs it first."""
    for item in items:
        if "valkey" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.timeout(ENGINE_TIMEOUT))
            break


@pytest.fixture(scope="session")
def valkey():
    ec = boto3.client("elasticache")
    group = "flags-" + uuid.uuid4().hex[:10]
    ec.create_replication_group(ReplicationGroupId=group, ReplicationGroupDescription="flag store",
                                Engine="valkey", CacheNodeType="cache.t3.micro", NumCacheClusters=1)
    try:
        deadline = time.time() + ENGINE_TIMEOUT - 60
        while True:
            described = ec.describe_replication_groups(ReplicationGroupId=group)["ReplicationGroups"][0]
            endpoint = ((described.get("NodeGroups") or [{}])[0]).get("PrimaryEndpoint") or {}
            if described.get("Status") == "available" and endpoint.get("Address"):
                address = (endpoint["Address"], int(endpoint["Port"]))
                try:
                    if redis.Redis(host=address[0], port=address[1], socket_timeout=3).ping():
                        break
                except redis.RedisError:
                    pass
            assert time.time() < deadline, f"replication group {group} never became usable"
            time.sleep(2)
        yield address
    finally:
        ec.delete_replication_group(ReplicationGroupId=group)


@pytest.fixture
def store(valkey):
    s = Store(valkey)
    try:
        yield s
    finally:
        s.cleanup()


@pytest.fixture
def queue():
    sqs = boto3.client("sqs")
    url = sqs.create_queue(QueueName=unique("flag-events-"))["QueueUrl"]
    try:
        yield url
    finally:
        sqs.delete_queue(QueueUrl=url)


@pytest.fixture
def scratch():
    with workspace() as directory:
        yield directory


@pytest.fixture
def launch(scratch):
    """launch(store=..., queue_url=..., **config) -> ready Relay; all are stopped at teardown."""
    started = []

    def start(store=None, queue_url=None, ready=True, **options):
        port = free_port()
        relay = Relay(scratch, config_text(port, store, queue_url, **options), port)
        started.append(relay)
        return relay.wait_ready() if ready else relay

    try:
        yield start
    finally:
        for relay in started:
            relay.stop()


@pytest.fixture(scope="module")
def module_relay(valkey, request):
    """A module-wide service over the module's FLAGS (and an SQS queue), for read-only scenarios."""
    flags = request.module.FLAGS
    s = Store(valkey)
    sqs = boto3.client("sqs")
    url = sqs.create_queue(QueueName=unique("flag-events-"))["QueueUrl"]
    with workspace() as directory:
        port = free_port()
        relay = None
        try:
            s.put_all(flags)
            relay = Relay(directory, config_text(port, s, url), port)
            relay.wait_ready()
            relay.queue_url = url
            yield relay
        finally:
            if relay is not None:
                relay.stop()
            s.cleanup()
            sqs.delete_queue(QueueUrl=url)
