import time
import uuid

import boto3
import pymysql
import pytest

import broker as support
from harness import workspace

MASTER_USER = "brokeradmin"
MASTER_PASSWORD = "broker-pass-7"
DATABASE = "brokerdb"


def pytest_collection_modifyitems(items):
    # The session database is created by whichever test runs first; its setup counts toward that test.
    if items:
        items[0].add_marker(pytest.mark.timeout(600))


@pytest.fixture(scope="session")
def mysql():
    rds = boto3.client("rds")
    name = "broker-" + uuid.uuid4().hex[:10]
    rds.create_db_instance(
        DBInstanceIdentifier=name, Engine="mysql", DBInstanceClass="db.t3.micro", AllocatedStorage=20,
        MasterUsername=MASTER_USER, MasterUserPassword=MASTER_PASSWORD, DBName=DATABASE,
    )
    try:
        deadline = time.time() + 480
        while True:
            instance = rds.describe_db_instances(DBInstanceIdentifier=name)["DBInstances"][0]
            if instance.get("DBInstanceStatus") == "available" and instance.get("Endpoint"):
                break
            assert time.time() < deadline, f"RDS instance not available: {instance.get('DBInstanceStatus')}"
            time.sleep(2)
        endpoint = instance["Endpoint"]
        info = {"host": endpoint["Address"], "port": int(endpoint["Port"]), "user": MASTER_USER,
                "password": MASTER_PASSWORD, "db": DATABASE}
        while True:  # the engine may still be initialising after 'available'
            try:
                with support.connect(info, database=False) as con, con.cursor() as cur:
                    cur.execute(f"CREATE DATABASE IF NOT EXISTS `{DATABASE}`")
                break
            except pymysql.err.OperationalError:
                assert time.time() < deadline, "MySQL engine never accepted logins"
                time.sleep(2)
        yield info
    finally:
        rds.delete_db_instance(DBInstanceIdentifier=name, SkipFinalSnapshot=True)


@pytest.fixture
def table(mysql):
    name = "q_" + uuid.uuid4().hex[:12]
    yield name
    support.drop_table(mysql, name)


@pytest.fixture
def make_broker(mysql):
    """Factory for servers on fresh ports; every server is stopped at teardown."""
    created = []
    with workspace() as directory:
        def make(table, *, start=True, equals=False, extra=()):
            server = support.Broker(mysql, table, directory, equals=equals, extra=extra)
            created.append(server)
            return server.start() if start else server

        try:
            yield make
        finally:
            for server in created:
                server.kill()


@pytest.fixture
def server(make_broker, table):
    return make_broker(table)
