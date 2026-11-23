import time
import uuid

import boto3
import psycopg
import pytest

from harness import workspace
from schema_sync import Context, Server


def pytest_collection_modifyitems(items):
    """The first test that provisions the RDS instance pays for its startup."""
    for item in items:
        if "ctx" in item.fixturenames or "rds" in item.fixturenames:
            item.add_marker(pytest.mark.timeout(600))
            break


@pytest.fixture(scope="session")
def rds():
    client = boto3.client("rds")
    identifier = "sync-" + uuid.uuid4().hex[:10]
    user, password = "syncadmin", "Sync-" + uuid.uuid4().hex[:12]
    client.create_db_instance(DBInstanceIdentifier=identifier, Engine="postgres", DBInstanceClass="db.t3.micro",
                              AllocatedStorage=20, MasterUsername=user, MasterUserPassword=password)
    try:
        deadline = time.time() + 480
        while True:
            instance = client.describe_db_instances(DBInstanceIdentifier=identifier)["DBInstances"][0]
            if instance.get("DBInstanceStatus") == "available" and instance.get("Endpoint", {}).get("Address"):
                break
            assert time.time() < deadline, f"RDS instance not available: {instance.get('DBInstanceStatus')}"
            time.sleep(2)
        server = Server(instance["Endpoint"]["Address"], instance["Endpoint"]["Port"], user, password)
        while True:
            try:
                server.execute("postgres", "SELECT 1")
                break
            except psycopg.OperationalError:
                assert time.time() < deadline, "RDS PostgreSQL does not accept connections"
                time.sleep(2)
        yield server
    finally:
        client.delete_db_instance(DBInstanceIdentifier=identifier, SkipFinalSnapshot=True)


@pytest.fixture
def ctx(rds):
    with workspace() as root:
        context = Context(rds, root)
        try:
            yield context
        finally:
            context.close()
