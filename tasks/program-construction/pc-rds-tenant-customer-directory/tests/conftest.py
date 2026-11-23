"""Shared fixtures: one RDS Postgres instance per session, one fresh database and server per test."""

import time
import uuid

import boto3
import psycopg
import pytest

from support import TENANT_INIT, Directory, admin_connect


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    # The first selected test pays for the session's RDS instance.
    if items:
        items[0].add_marker(pytest.mark.timeout(600))


@pytest.fixture(scope="session")
def rds():
    client = boto3.client("rds")
    name = "dir-" + uuid.uuid4().hex[:10]
    client.create_db_instance(
        DBInstanceIdentifier=name,
        Engine="postgres",
        DBInstanceClass="db.t3.micro",
        MasterUsername="app",
        MasterUserPassword="app-pass-1",
        AllocatedStorage=20,
    )
    try:
        client.get_waiter("db_instance_available").wait(DBInstanceIdentifier=name, WaiterConfig={"Delay": 2, "MaxAttempts": 200})
        endpoint = client.describe_db_instances(DBInstanceIdentifier=name)["DBInstances"][0]["Endpoint"]
        info = {"host": endpoint["Address"], "port": int(endpoint["Port"]), "user": "app", "password": "app-pass-1"}
        deadline = time.time() + 120
        while True:
            try:
                with admin_connect(info):
                    break
            except psycopg.OperationalError:
                if time.time() > deadline:
                    raise
                time.sleep(1)
        yield info
    finally:
        client.delete_db_instance(DBInstanceIdentifier=name, SkipFinalSnapshot=True)


@pytest.fixture
def directory(rds):
    """A fresh database, provisioned with the two-tenant init data, server started with -createDatabase."""
    with Directory(rds) as d:
        d.write_files(TENANT_INIT)
        d.start("-createDatabase")
        yield d


@pytest.fixture
def unstarted(rds):
    """A fresh database name and workspace whose server the test starts itself."""
    with Directory(rds) as d:
        yield d


@pytest.fixture
def launch(rds):
    """Start servers on fresh databases from given init data; all are stopped and dropped after the test."""
    started = []

    def start(init):
        d = Directory(rds)
        d.__enter__()
        started.append(d)
        d.write_files(init)
        d.start("-createDatabase")
        return d

    yield start
    for d in reversed(started):
        d.__exit__(None, None, None)


@pytest.fixture
def bucket():
    """A fresh S3 bucket on the venue endpoint (an underscore keeps the name off virtual-host addressing)."""
    s3 = boto3.client("s3")
    name = "files_" + uuid.uuid4().hex[:12]
    s3.create_bucket(Bucket=name)
    yield name
    try:
        for item in s3.list_objects_v2(Bucket=name).get("Contents", []):
            s3.delete_object(Bucket=name, Key=item["Key"])
        s3.delete_bucket(Bucket=name)
    except Exception:  # noqa: BLE001 -- cleanup only
        pass
