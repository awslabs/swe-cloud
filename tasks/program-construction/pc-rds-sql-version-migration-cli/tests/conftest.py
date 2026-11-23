"""Shared fixtures: one RDS Postgres engine per session, a fresh database per test."""

import contextlib
import time
import uuid

import boto3
import psycopg
import pytest
from harness import workspace

from support import Bucket, Database

USER, PASSWORD = "app", "app-pass-1"


def pytest_configure(config):
    config.addinivalue_line("filterwarnings", "ignore::DeprecationWarning:botocore")


def _uses_engine(item):
    return "engine" in getattr(item, "fixturenames", ())


def pytest_collection_modifyitems(items):
    """Engine provisioning counts toward the first engine-backed test's timeout."""
    first = next((item for item in items if _uses_engine(item)), None)
    if first is not None:
        first.add_marker(pytest.mark.timeout(600))


class Engine:
    def __init__(self, host, port):
        self.host, self.port = host, port

    def connect(self, dbname="postgres", **options):
        return psycopg.connect(host=self.host, port=self.port, user=USER, password=PASSWORD, dbname=dbname, **options)

    def create_database(self):
        name = "t" + uuid.uuid4().hex[:12]
        with self.connect(autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{name}"')
        return Database(self, name)

    def drop_database(self, name):
        with contextlib.suppress(Exception), self.connect(autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture(scope="session")
def engine():
    rds = boto3.client("rds")
    name = "svr-" + uuid.uuid4().hex[:10]
    rds.create_db_instance(
        DBInstanceIdentifier=name,
        Engine="postgres",
        DBInstanceClass="db.t3.micro",
        MasterUsername=USER,
        MasterUserPassword=PASSWORD,
        AllocatedStorage=20,
    )
    try:
        rds.get_waiter("db_instance_available").wait(DBInstanceIdentifier=name, WaiterConfig={"Delay": 2, "MaxAttempts": 250})
        endpoint = rds.describe_db_instances(DBInstanceIdentifier=name)["DBInstances"][0]["Endpoint"]
        result = Engine(endpoint["Address"], endpoint["Port"])
        deadline = time.monotonic() + 240
        while True:
            try:
                result.connect().close()
                break
            except psycopg.OperationalError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(1)
        yield result
    finally:
        with contextlib.suppress(Exception):
            rds.delete_db_instance(DBInstanceIdentifier=name, SkipFinalSnapshot=True)


@pytest.fixture
def db(engine):
    database = engine.create_database()
    try:
        yield database
    finally:
        engine.drop_database(database.name)


@pytest.fixture
def schema_role(engine, db):
    """A second login role; its database-level settings are set by the test."""
    name = "r" + uuid.uuid4().hex[:12]
    password = "role-pass-1"
    with engine.connect(autocommit=True) as conn:
        conn.execute(f'CREATE ROLE "{name}" LOGIN PASSWORD \'{password}\'')
    try:
        yield name, password
    finally:
        engine.drop_database(db.name)  # removes the objects and settings tied to the role first
        with contextlib.suppress(Exception), engine.connect(autocommit=True) as conn:
            conn.execute(f'DROP ROLE IF EXISTS "{name}"')


@pytest.fixture
def ws():
    with workspace() as root:
        yield root


@pytest.fixture
def bucket():
    created = Bucket()
    try:
        yield created
    finally:
        created.destroy()
