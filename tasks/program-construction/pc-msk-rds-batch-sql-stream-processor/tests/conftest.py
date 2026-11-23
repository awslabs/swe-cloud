"""Session engines (MSK Kafka and RDS Postgres through Floci) and per-test streams."""

import time
import uuid
from dataclasses import dataclass

import boto3
import psycopg
import pytest

from harness import workspace
from streams import Kafka, Stream

ENGINE_FIXTURES = {"venue", "stream"}


def pytest_collection_modifyitems(items):
    # Engine provisioning counts toward the first test that needs it.
    for item in items:
        if ENGINE_FIXTURES & set(item.fixturenames):
            item.add_marker(pytest.mark.timeout(600))
            break


@dataclass
class Venue:
    brokers: str
    dsn: str
    kafka: Kafka


def _wait(check, timeout, what):
    deadline = time.monotonic() + timeout
    while True:
        try:
            value = check()
        except Exception as error:  # noqa: BLE001 -- engines refuse connections while starting
            value, last = None, error
        else:
            last = None
        if value:
            return value
        if time.monotonic() > deadline:
            raise RuntimeError(f"{what} did not become ready: {last}")
        time.sleep(2)


@pytest.fixture(scope="session")
def venue():
    name = "v" + uuid.uuid4().hex[:10]
    msk, rds = boto3.client("kafka"), boto3.client("rds")
    cluster = None
    database = False
    try:
        cluster = msk.create_cluster(
            ClusterName=name, KafkaVersion="3.6.1", NumberOfBrokerNodes=1,
            BrokerNodeGroupInfo={"InstanceType": "kafka.m5.large", "ClientSubnets": ["subnet-1"]},
        )["ClusterArn"]
        rds.create_db_instance(
            DBInstanceIdentifier=name, Engine="postgres", DBInstanceClass="db.t3.micro",
            MasterUsername="app", MasterUserPassword="app-pass-1", AllocatedStorage=20, DBName="sink",
        )
        database = True
        _wait(lambda: msk.describe_cluster(ClusterArn=cluster)["ClusterInfo"]["State"] == "ACTIVE", 400, "MSK cluster")
        brokers = msk.get_bootstrap_brokers(ClusterArn=cluster)["BootstrapBrokerString"]

        def endpoint():
            instance = rds.describe_db_instances(DBInstanceIdentifier=name)["DBInstances"][0]
            return instance["DBInstanceStatus"] == "available" and instance.get("Endpoint")

        address = _wait(endpoint, 400, "RDS instance")
        dsn = f"postgresql://app:app-pass-1@{address['Address']}:{address['Port']}/sink?sslmode=disable"
        _wait(lambda: psycopg.connect(dsn, connect_timeout=5).close() or True, 120, "Postgres")
        kafka = Kafka(brokers)
        _wait(lambda: kafka.admin.list_topics(timeout=5) is not None, 120, "Kafka brokers")
        yield Venue(brokers, dsn, kafka)
    finally:
        if cluster:
            try:
                msk.delete_cluster(ClusterArn=cluster)
            except Exception:  # noqa: BLE001 -- teardown is best effort
                pass
        if database:
            try:
                rds.delete_db_instance(DBInstanceIdentifier=name, SkipFinalSnapshot=True)
            except Exception:  # noqa: BLE001
                pass


@pytest.fixture
def stream(venue):
    with workspace() as root:
        current = Stream(venue, root)
        try:
            yield current
        finally:
            current.cleanup()


@pytest.fixture
def scratch():
    with workspace() as root:
        yield root
