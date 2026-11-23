import time
import uuid

import boto3
import psycopg
import pytest

from flagr_support import PG_PASSWORD, PG_USER, Server


def pytest_collection_modifyitems(items):
    # The shared RDS instance is created by whichever test first needs it.
    for item in items:
        if "postgres" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.timeout(600))
            break


@pytest.fixture(scope="session")
def postgres():
    rds = boto3.client("rds")
    ident = "flagr-" + uuid.uuid4().hex[:10]
    rds.create_db_instance(DBInstanceIdentifier=ident, Engine="postgres", DBInstanceClass="db.t3.micro",
                           MasterUsername=PG_USER, MasterUserPassword=PG_PASSWORD, AllocatedStorage=20)
    try:
        rds.get_waiter("db_instance_available").wait(DBInstanceIdentifier=ident, WaiterConfig={"Delay": 2, "MaxAttempts": 250})
        endpoint = rds.describe_db_instances(DBInstanceIdentifier=ident)["DBInstances"][0]["Endpoint"]
        info = {"host": endpoint["Address"], "port": endpoint["Port"]}
        deadline = time.time() + 120
        while True:
            try:
                with psycopg.connect(host=info["host"], port=info["port"], user=PG_USER, password=PG_PASSWORD,
                                     dbname="postgres", connect_timeout=5):
                    break
            except psycopg.OperationalError:
                if time.time() > deadline:
                    raise
                time.sleep(1)
        yield info
    finally:
        try:
            rds.delete_db_instance(DBInstanceIdentifier=ident, SkipFinalSnapshot=True)
        except Exception:  # noqa: BLE001 -- best-effort cleanup
            pass


@pytest.fixture
def flagr(postgres):
    """Factory: flagr(env=..., recorder=..., refresh=...) starts a server on a fresh database."""
    servers = []

    def start(**options):
        server = Server(postgres, **options)
        servers.append(server)
        return server.start()

    yield start
    for server in servers:
        server.close()
