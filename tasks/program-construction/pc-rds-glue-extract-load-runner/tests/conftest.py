"""Session RDS PostgreSQL engine and a fresh, cleaned-up resource set per test."""

import time
import uuid

import boto3
import psycopg
import pytest

from harness import workspace
from synclab import PASSWORD, Lab


def pytest_collection_modifyitems(items):
    # The engine is created by the first test that requests it; give that test room.
    for item in items:
        if "lab" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.timeout(600))
            break


@pytest.fixture(scope="session")
def engine():
    rds = boto3.client("rds")
    name = "sync-" + uuid.uuid4().hex[:10]
    rds.create_db_instance(DBInstanceIdentifier=name, Engine="postgres", DBInstanceClass="db.t3.micro",
                           MasterUsername="app", MasterUserPassword=PASSWORD, AllocatedStorage=20)
    try:
        deadline = time.time() + 540
        while True:
            inst = rds.describe_db_instances(DBInstanceIdentifier=name)["DBInstances"][0]
            if inst.get("DBInstanceStatus") == "available" and inst.get("Endpoint"):
                break
            assert time.time() < deadline, "RDS instance did not become available"
            time.sleep(2)
        host, port = inst["Endpoint"]["Address"], inst["Endpoint"]["Port"]
        while True:
            try:
                psycopg.connect(host=host, port=port, user="app", password=PASSWORD, dbname="postgres",
                                sslmode="disable", connect_timeout=5).close()
                break
            except psycopg.OperationalError:
                assert time.time() < deadline, "PostgreSQL engine did not accept connections"
                time.sleep(2)
        yield {"host": host, "port": port}
    finally:
        rds.delete_db_instance(DBInstanceIdentifier=name, SkipFinalSnapshot=True)


@pytest.fixture
def lab(engine):
    with workspace() as root:
        lab = Lab(engine, root)
        try:
            lab.setup()
            yield lab
        finally:
            lab.cleanup()


