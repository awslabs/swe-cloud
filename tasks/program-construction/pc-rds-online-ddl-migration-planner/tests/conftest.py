"""Fixtures: one RDS PostgreSQL instance per session, fresh databases and schema dirs per test."""

import time
import uuid
from pathlib import Path

import boto3
import psycopg
import pytest

from harness import give_to_candidate, workspace, write_fixture

MASTER_USER = "app"
MASTER_PASSWORD = "verify-pass-7"


def pytest_collection_modifyitems(items):
    # The first test also pays for creating the RDS instance.
    if items:
        items[0].add_marker(pytest.mark.timeout(600))


class Database:
    def __init__(self, server, name):
        self.server = server
        self.name = name
        self.dsn = server.dsn(name)

    def connect(self):
        return self.server.connect(self.name)

    def execute(self, sql, params=None):
        with self.connect() as conn:
            conn.execute(sql, params)

    def query(self, sql, params=None):
        with self.connect() as conn:
            return conn.execute(sql, params).fetchall()

    def scalar(self, sql, params=None):
        return self.query(sql, params)[0][0]


class Server:
    def __init__(self, host, port):
        self.host = host
        self.port = port

    def dsn(self, database, user=MASTER_USER, password=MASTER_PASSWORD):
        credentials = f"{user}:{password}" if password is not None else user
        return f"postgres://{credentials}@{self.host}:{self.port}/{database}"

    def connect(self, database, attempts=60):
        for attempt in range(attempts):
            try:
                return psycopg.connect(host=self.host, port=self.port, user=MASTER_USER, password=MASTER_PASSWORD,
                                       dbname=database, autocommit=True, connect_timeout=5)
            except psycopg.OperationalError:
                if attempt == attempts - 1:
                    raise
                time.sleep(1)

    def databases(self):
        with self.connect("postgres") as conn:
            return {row[0] for row in conn.execute("select datname from pg_database").fetchall()}


@pytest.fixture(scope="session")
def server():
    rds = boto3.client("rds")
    name = "verify-" + uuid.uuid4().hex[:10]
    rds.create_db_instance(DBInstanceIdentifier=name, Engine="postgres", DBInstanceClass="db.t3.micro",
                           MasterUsername=MASTER_USER, MasterUserPassword=MASTER_PASSWORD, AllocatedStorage=20)
    try:
        deadline = time.time() + 500
        while True:
            instance = rds.describe_db_instances(DBInstanceIdentifier=name)["DBInstances"][0]
            if instance.get("DBInstanceStatus") == "available" and instance.get("Endpoint"):
                break
            assert time.time() < deadline, f"RDS instance not available: {instance.get('DBInstanceStatus')}"
            time.sleep(2)
        endpoint = instance["Endpoint"]
        srv = Server(endpoint["Address"], endpoint["Port"])
        srv.connect("postgres", attempts=120).close()
        yield srv
    finally:
        rds.delete_db_instance(DBInstanceIdentifier=name, SkipFinalSnapshot=True)


@pytest.fixture
def make_db(server):
    """Factory for fresh, empty databases (optionally seeded with DDL); all dropped at teardown."""
    created = []

    def make(ddl=None):
        name = "t_" + uuid.uuid4().hex[:12]
        with server.connect("postgres") as conn:
            conn.execute(f"create database {name}")
        created.append(name)
        db = Database(server, name)
        if ddl:
            db.execute(ddl)
        return db

    yield make
    with server.connect("postgres") as conn:
        for name in created:
            conn.execute(f"drop database if exists {name} with (force)")


@pytest.fixture
def db(make_db):
    return make_db()


@pytest.fixture
def scratch():
    with workspace() as root:
        yield root


@pytest.fixture
def schema_dir(scratch):
    """Factory writing {relative path: text} into a fresh candidate-readable directory."""
    counter = iter(range(1000))

    def make(files):
        root = Path(scratch) / f"schema{next(counter)}"
        root.mkdir(mode=0o755)
        give_to_candidate(root)
        for relative, text in files.items():
            write_fixture(root / relative, text, mode=0o644)
        return root

    return make


@pytest.fixture
def oracle(make_db):
    """Catalog of a database built by running the target DDL directly (PostgreSQL is the oracle)."""
    from planlib import catalog

    def build(ddl):
        return catalog(make_db(ddl))

    return build
