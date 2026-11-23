import time
import uuid

import boto3
import psycopg
import pytest

from harness import workspace
from plikenv import PlikEnv


def pytest_collection_modifyitems(items):
    # The first test also pays for the session's RDS engine creation.
    if items:
        items[0].add_marker(pytest.mark.timeout(600))


@pytest.fixture(scope="session")
def engine():
    """One RDS PostgreSQL instance for the session; each test gets its own database inside it."""
    rds = boto3.client("rds")
    name = "plik-" + uuid.uuid4().hex[:10]
    password = "verifier-pass-1"
    rds.create_db_instance(
        DBInstanceIdentifier=name,
        Engine="postgres",
        DBInstanceClass="db.t3.micro",
        MasterUsername="app",
        MasterUserPassword=password,
        AllocatedStorage=20,
    )
    try:
        rds.get_waiter("db_instance_available").wait(DBInstanceIdentifier=name, WaiterConfig={"Delay": 2, "MaxAttempts": 200})
        endpoint = rds.describe_db_instances(DBInstanceIdentifier=name)["DBInstances"][0]["Endpoint"]
        dsn = f"host={endpoint['Address']} port={endpoint['Port']} user=app password={password} sslmode=disable connect_timeout=5"
        deadline = time.time() + 300
        while True:
            try:
                psycopg.connect(dsn + " dbname=postgres").close()
                break
            except psycopg.OperationalError:
                if time.time() > deadline:
                    raise
                time.sleep(2)
        yield {"dsn": dsn}
    finally:
        rds.delete_db_instance(DBInstanceIdentifier=name, SkipFinalSnapshot=True)


@pytest.fixture
def plik(engine):
    """A fresh database, bucket and TOML configuration; tests provision users and start the server."""
    with workspace() as directory:
        env = PlikEnv(engine, directory)
        try:
            yield env
        finally:
            env.close()


@pytest.fixture
def pair(plik):
    """Server running with two tenants (64-byte limits), each logged in with one API token."""
    plik.add_user("alice", "alice-secret-1", "64B")
    plik.add_user("bobby", "bobby-secret-2", "64B")
    plik.start()
    return plik.tenant("alice", "alice-secret-1"), plik.tenant("bobby", "bobby-secret-2")


@pytest.fixture
def open_pair(plik):
    """Server running with two unlimited tenants."""
    plik.add_user("alice", "alice-secret-1", "-1")
    plik.add_user("bobby", "bobby-secret-2", "-1")
    plik.start()
    return plik.tenant("alice", "alice-secret-1"), plik.tenant("bobby", "bobby-secret-2")
