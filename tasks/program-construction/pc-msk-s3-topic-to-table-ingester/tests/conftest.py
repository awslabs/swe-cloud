import time
import uuid

import boto3
import pytest
from harness import workspace
from lake import Lake


def pytest_collection_modifyitems(items):
    """Whichever selected test first needs the MSK cluster also pays for creating it."""
    for item in items:
        if "brokers" in item.fixturenames:
            if item.get_closest_marker("timeout") is None:
                item.add_marker(pytest.mark.timeout(600))
            return


@pytest.fixture(scope="session")
def brokers():
    """One MSK cluster for the session; the program receives its bootstrap broker string."""
    kafka = boto3.client("kafka")
    arn = kafka.create_cluster(
        ClusterName="ingest-" + uuid.uuid4().hex[:8], KafkaVersion="3.6.0", NumberOfBrokerNodes=1,
        BrokerNodeGroupInfo={"InstanceType": "kafka.m5.large", "ClientSubnets": ["subnet-1"]},
    )["ClusterArn"]
    try:
        deadline = time.time() + 400
        while kafka.describe_cluster(ClusterArn=arn)["ClusterInfo"]["State"] != "ACTIVE":
            assert time.time() < deadline, "MSK cluster did not become ACTIVE"
            time.sleep(2)
        yield kafka.get_bootstrap_brokers(ClusterArn=arn)["BootstrapBrokerString"]
    finally:
        try:
            kafka.delete_cluster(ClusterArn=arn)
        except Exception:
            pass


@pytest.fixture(scope="session")
def lock_table():
    ddb = boto3.client("dynamodb")
    name = "ingest-locks-" + uuid.uuid4().hex[:8]
    ddb.create_table(
        TableName=name,
        AttributeDefinitions=[{"AttributeName": "tablePath", "AttributeType": "S"}, {"AttributeName": "fileName", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "tablePath", "KeyType": "HASH"}, {"AttributeName": "fileName", "KeyType": "RANGE"}],
        BillingMode="PAY_PER_REQUEST",
    )
    try:
        yield name
    finally:
        try:
            ddb.delete_table(TableName=name)
        except Exception:
            pass


@pytest.fixture
def lake(brokers, lock_table):
    with workspace() as cwd:
        env = Lake(brokers, lock_table, cwd)
        try:
            yield env
        finally:
            env.close()
