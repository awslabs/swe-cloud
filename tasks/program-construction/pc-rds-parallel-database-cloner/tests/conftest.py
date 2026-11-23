"""Shared fixtures: one source and one target RDS PostgreSQL instance per session."""

import time
import uuid

import boto3
import psycopg
import pytest

from dbtools import Instance, drop_database
from harness import workspace

MASTER_USER = "cloneadmin"
MASTER_PASSWORD = "clone-pw-" + uuid.uuid4().hex[:8]


def _provision(identifiers):
    rds = boto3.client("rds")
    for identifier in identifiers:
        rds.create_db_instance(
            DBInstanceIdentifier=identifier,
            Engine="postgres",
            DBInstanceClass="db.t3.micro",
            AllocatedStorage=20,
            MasterUsername=MASTER_USER,
            MasterUserPassword=MASTER_PASSWORD,
        )
    instances = []
    deadline = time.monotonic() + 540
    for identifier in identifiers:
        while True:
            described = rds.describe_db_instances(DBInstanceIdentifier=identifier)["DBInstances"][0]
            if described.get("DBInstanceStatus") == "available" and described.get("Endpoint"):
                endpoint = described["Endpoint"]
                instances.append(Instance(endpoint["Address"], int(endpoint["Port"]), MASTER_USER, MASTER_PASSWORD))
                break
            if time.monotonic() > deadline:
                raise RuntimeError(f"RDS instance {identifier} did not become available")
            time.sleep(2)
    for instance in instances:
        while True:
            try:
                with psycopg.connect(instance.uri("postgres"), connect_timeout=5) as conn:
                    conn.execute("select 1")
                break
            except psycopg.OperationalError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(1)
    return instances


@pytest.fixture(scope="session")
def instances():
    suffix = uuid.uuid4().hex[:8]
    identifiers = [f"clone-src-{suffix}", f"clone-dst-{suffix}"]
    try:
        yield _provision(identifiers)
    finally:
        rds = boto3.client("rds")
        for identifier in identifiers:
            try:
                rds.delete_db_instance(DBInstanceIdentifier=identifier, SkipFinalSnapshot=True)
            except Exception:  # noqa: BLE001 -- best-effort cleanup of our own instances
                pass


@pytest.fixture(scope="session")
def source_instance(instances):
    return instances[0]


@pytest.fixture(scope="session")
def target_instance(instances):
    return instances[1]


@pytest.fixture
def make_db(instances):
    """Create fresh databases on either instance; all are dropped at teardown."""
    created = []

    def make(instance, prefix="db"):
        name = f"{prefix}_{uuid.uuid4().hex[:10]}"
        with psycopg.connect(instance.uri("postgres"), autocommit=True) as conn:
            conn.execute(f"create database {name}")
        created.append((instance, name))
        return instance.uri(name)

    yield make
    for instance, name in reversed(created):
        drop_database(instance, name)


@pytest.fixture
def source_db(make_db, source_instance):
    return make_db(source_instance, "src")


@pytest.fixture
def target_db(make_db, target_instance):
    return make_db(target_instance, "dst")


@pytest.fixture
def work():
    with workspace() as root:
        yield root
