import time
import uuid

import boto3
import psycopg
import pytest

from harness import workspace
from mailer import Database, quiet_smtp, start_app


class Engine:
    """One RDS Postgres instance for the session; every test gets its own database on it."""

    def __init__(self):
        self.rds = boto3.client("rds")
        self.id = "mailer-" + uuid.uuid4().hex[:10]
        self.user, self.password = "mailadmin", "mailpass-" + uuid.uuid4().hex[:8]
        self.rds.create_db_instance(DBInstanceIdentifier=self.id, Engine="postgres", DBInstanceClass="db.t3.micro",
                                    MasterUsername=self.user, MasterUserPassword=self.password, AllocatedStorage=20)
        deadline = time.time() + 500
        while True:
            instance = self.rds.describe_db_instances(DBInstanceIdentifier=self.id)["DBInstances"][0]
            if instance.get("DBInstanceStatus") == "available" and instance.get("Endpoint", {}).get("Address"):
                break
            assert time.time() < deadline, f"RDS instance never became available: {instance.get('DBInstanceStatus')}"
            time.sleep(2)
        self.host, self.port = instance["Endpoint"]["Address"], instance["Endpoint"]["Port"]
        while True:
            try:
                self.admin = psycopg.connect(host=self.host, port=self.port, user=self.user, password=self.password,
                                             dbname="postgres", autocommit=True, connect_timeout=5)
                break
            except psycopg.OperationalError:
                assert time.time() < deadline, "RDS Postgres never accepted connections"
                time.sleep(2)

    def create_database(self):
        name = "mailer_" + uuid.uuid4().hex[:12]
        self.admin.execute(f'CREATE DATABASE "{name}"')
        return name

    def drop_database(self, name):
        self.admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')

    def close(self):
        self.admin.close()
        self.rds.delete_db_instance(DBInstanceIdentifier=self.id, SkipFinalSnapshot=True)


def pytest_collection_modifyitems(items):
    # Session setup (the RDS instance) is charged to whichever test runs first.
    if items:
        items[0].add_marker(pytest.mark.timeout(600))


@pytest.fixture(scope="session")
def engine():
    instance = Engine()
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def database(engine):
    """An empty database and a candidate workspace whose config.toml points at it."""
    with workspace() as root:
        db = Database(engine, root)
        try:
            yield db
        finally:
            db.close()


@pytest.fixture
def bare_app(database):
    """Installed and serving, default settings."""
    app = start_app(database)
    try:
        yield app
    finally:
        app.stop()


@pytest.fixture
def app(bare_app):
    """Installed and serving, with SMTP pointed at a closed local port."""
    bare_app.configure(quiet_smtp)
    return bare_app


@pytest.fixture
def bucket():
    s3 = boto3.client("s3")
    name = "mailer-" + uuid.uuid4().hex[:12]
    s3.create_bucket(Bucket=name)
    try:
        yield name
    finally:
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=name):
            for item in page.get("Contents", []):
                s3.delete_object(Bucket=name, Key=item["Key"])
        s3.delete_bucket(Bucket=name)
