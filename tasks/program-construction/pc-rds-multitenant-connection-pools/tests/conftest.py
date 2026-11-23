"""Shared fixtures: RDS PostgreSQL engines, per-test tenant databases and the pooler process."""

import os
import secrets
import socket
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager

import boto3
import psycopg
import pytest

from harness import start_command, workspace, write_fixture
from floci_venue import Watcher, _bridge

os.environ.setdefault("PGSSLMODE", "disable")
os.environ.setdefault("PGGSSENCMODE", "disable")

MASTER_USER = "suitemaster"


def pytest_collection_modifyitems(items):
    # The first test pays for creating the session's RDS instances.
    if items:
        items[0].add_marker(pytest.mark.timeout(600))


def _connect(host, port, user, password, dbname, **kwargs):
    return psycopg.connect(host=host, port=port, user=user, password=password, dbname=dbname,
                           autocommit=True, connect_timeout=10, sslmode="disable", **kwargs)


class Engine:
    """One RDS-created PostgreSQL instance: its endpoint for inspection and its engine address for the pooler."""

    def __init__(self, ident, host, port, master_password):
        self.ident, self.host, self.port, self.master_password = ident, host, port, master_password
        self.engine_host = self.engine_port = None

    def master(self, dbname="postgres"):
        return _connect(self.host, self.port, MASTER_USER, self.master_password, dbname)

    def direct(self, dbname, user, password):
        return _connect(self.host, self.port, user, password, dbname)

    def query(self, sql, dbname="postgres"):
        with self.master(dbname) as conn:
            return conn.execute(sql).fetchall()


@pytest.fixture(scope="session")
def engines():
    rds = boto3.client("rds")
    tag = uuid.uuid4().hex[:8]
    password = "master-" + secrets.token_hex(8)
    idents = [f"pools-{tag}-1", f"pools-{tag}-2"]
    created = []
    relays = Watcher()
    try:
        for ident in idents:
            rds.create_db_instance(DBInstanceIdentifier=ident, Engine="postgres", DBInstanceClass="db.t3.micro",
                                   AllocatedStorage=20, MasterUsername=MASTER_USER, MasterUserPassword=password)
            created.append(ident)
        result = []
        for ident in idents:
            rds.get_waiter("db_instance_available").wait(DBInstanceIdentifier=ident,
                                                         WaiterConfig={"Delay": 2, "MaxAttempts": 150})
            endpoint = rds.describe_db_instances(DBInstanceIdentifier=ident)["DBInstances"][0]["Endpoint"]
            engine = Engine(ident, endpoint["Address"], endpoint["Port"], password)
            deadline = time.time() + 120
            while True:
                try:
                    with engine.master() as conn:
                        for grant in ("pg_read_all_stats", "pg_signal_backend"):
                            try:
                                conn.execute(f"GRANT {grant} TO CURRENT_USER")
                            except psycopg.Error:
                                pass
                        engine.engine_host, engine.engine_port = conn.execute(
                            "SELECT host(inet_server_addr()), inet_server_port()").fetchone()
                    # The venue network is internal, so the engine's bridge address is reachable only through the relay.
                    if _bridge(engine.engine_host):
                        relays.relay(engine.engine_host, engine.engine_port)
                    break
                except psycopg.OperationalError:
                    if time.time() > deadline:
                        raise
                    time.sleep(1)
            result.append(engine)
        yield result
    finally:
        for ident in created:
            try:
                rds.delete_db_instance(DBInstanceIdentifier=ident, SkipFinalSnapshot=True)
            except Exception:  # noqa: BLE001 -- best-effort cleanup
                pass


@pytest.fixture
def engine(engines):
    return engines[0]


class Tenant:
    def __init__(self, prefix, name):
        self.name = name
        self.owner = f"{prefix}_{name}_owner"
        self.owner_password = f"{name}-backend-" + secrets.token_hex(4)
        self.database = f"{prefix}_{name}_db"


class Tenants:
    """Two tenants (alpha, beta) with their own least-privileged owner role and physical database."""

    def __init__(self, engines, prefix):
        self.engines = engines
        self.prefix = prefix
        self.alpha = Tenant(prefix, "alpha")
        self.beta = Tenant(prefix, "beta")
        self.extra_roles = []
        self.extra_databases = []

    def __iter__(self):
        return iter((self.alpha, self.beta))

    def provision(self, engine, label=""):
        with engine.master() as conn:
            for tenant in self:
                conn.execute(f"CREATE ROLE {tenant.owner} LOGIN PASSWORD '{tenant.owner_password}'")
                conn.execute(f"GRANT {tenant.owner} TO CURRENT_USER")
                conn.execute(f"CREATE DATABASE {tenant.database} OWNER {tenant.owner}")
                conn.execute(f"REVOKE CONNECT ON DATABASE {tenant.database} FROM PUBLIC")
        for tenant in self:
            with self.owner_conn(tenant, engine) as conn:
                conn.execute("CREATE TABLE marker(tenant text)")
                conn.execute(f"INSERT INTO marker VALUES ('{tenant.name}{label}')")
                conn.execute("CREATE TABLE ledger(k text PRIMARY KEY, v int)")

    def owner_conn(self, tenant, engine=None):
        engine = engine or self.engines[0]
        return engine.direct(tenant.database, tenant.owner, tenant.owner_password)

    def add_role(self, suffix, password, tenant):
        """A login role (name prefixed) allowed to read the tenant's marker table."""
        role = f"{self.prefix}_{suffix}"
        engine = self.engines[0]
        with engine.master() as conn:
            conn.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'")
            conn.execute(f"GRANT CONNECT ON DATABASE {tenant.database} TO {role}")
        with self.owner_conn(tenant) as conn:
            conn.execute(f"GRANT SELECT ON marker TO {role}")
        self.extra_roles.append(role)
        return role

    def shard_databases(self, count, engine=None):
        """`count` extra databases owned by tenant alpha, each with marker ('s<index>') and an empty data(id, v) table."""
        engine = engine or self.engines[0]
        names = [f"{self.prefix}_s{index}" for index in range(count)]
        with engine.master() as conn:
            for name in names:
                conn.execute(f"CREATE DATABASE {name} OWNER {self.alpha.owner}")
                self.extra_databases.append(name)
        for index, name in enumerate(names):
            with engine.direct(name, self.alpha.owner, self.alpha.owner_password) as conn:
                conn.execute("CREATE TABLE marker(tenant text)")
                conn.execute(f"INSERT INTO marker VALUES ('s{index}')")
                conn.execute("CREATE TABLE data(id bigint, v text)")
        return names

    def ledger(self, tenant, engine=None):
        with self.owner_conn(tenant, engine) as conn:
            return sorted(conn.execute("SELECT k, v FROM ledger").fetchall())

    def activity(self, *roles):
        """(usename, datname, state) of every server connection the given roles hold, from pg_stat_activity."""
        roles = roles or (self.alpha.owner, self.beta.owner)
        names = ",".join(f"'{role}'" for role in roles)
        return sorted(self.engines[0].query(
            f"SELECT usename::text, datname::text, coalesce(state, '') FROM pg_stat_activity WHERE usename IN ({names})"))

    def drop(self, engine):
        with engine.master() as conn:
            for database in [t.database for t in self] + self.extra_databases:
                conn.execute(f"DROP DATABASE IF EXISTS {database} WITH (FORCE)")
            for role in [t.owner for t in self] + self.extra_roles:
                try:
                    conn.execute(f"DROP ROLE IF EXISTS {role}")
                except psycopg.Error:
                    pass


@pytest.fixture
def tenants(engines):
    prefix = "t" + uuid.uuid4().hex[:10]
    t = Tenants(engines, prefix)
    provisioned = []
    try:
        t.provision(engines[0])
        provisioned.append(engines[0])
        yield t
    finally:
        for engine in engines:
            try:
                t.drop(engine)
            except psycopg.Error:
                pass


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise TypeError(value)


def render_config(general, pools):
    """TOML text for a pooler configuration: general settings and {alias: {settings, users, servers, database}}.

    A pool may instead list several shards as `shards: [{servers, database}, ...]`.
    """
    lines = ["[general]"]
    lines += [f"{key} = {_toml_value(value)}" for key, value in general.items()]
    for alias, pool in pools.items():
        lines += ["", f"[pools.{alias}]"]
        lines += [f"{key} = {_toml_value(value)}" for key, value in pool.get("settings", {}).items()]
        for index, user in enumerate(pool["users"]):
            lines += ["", f"[pools.{alias}.users.{index}]"]
            lines += [f"{key} = {_toml_value(value)}" for key, value in user.items()]
        shards = pool.get("shards") or [{"servers": pool["servers"], "database": pool["database"]}]
        for index, shard in enumerate(shards):
            lines += ["", f"[pools.{alias}.shards.{index}]",
                      f"servers = {_toml_value(shard['servers'])}",
                      f"database = {_toml_value(shard['database'])}"]
    return "\n".join(lines) + "\n"


def tenant_pools(tenants, alpha_size=2, beta_size=3, alpha_users=(), alpha_servers=None, alpha_settings=None):
    """The standard two-tenant configuration: alias alpha -> tenant alpha, alias beta -> tenant beta, user app."""
    engine = tenants.engines[0]
    primary = [[engine.engine_host, engine.engine_port, "primary"]]
    return {
        "alpha": {
            "settings": {"pool_mode": "transaction", **(alpha_settings or {})},
            "users": [{"username": "app", "password": "alpha-secret", "server_username": tenants.alpha.owner,
                       "server_password": tenants.alpha.owner_password, "pool_size": alpha_size}, *alpha_users],
            "servers": alpha_servers or primary,
            "database": tenants.alpha.database,
        },
        "beta": {
            "settings": {"pool_mode": "transaction"},
            "users": [{"username": "app", "password": "beta-secret", "server_username": tenants.beta.owner,
                       "server_password": tenants.beta.owner_password, "pool_size": beta_size}],
            "servers": primary,
            "database": tenants.beta.database,
        },
    }


def wait_for_port(port, process, deadline=15):
    end = time.time() + deadline
    while time.time() < end:
        if process.poll() is not None:
            return False
        try:
            socket.create_connection(("127.0.0.1", port), 0.5).close()
            return True
        except OSError:
            time.sleep(0.1)
    return False


class Pooler:
    def __init__(self, port, process, directory, log, general=None):
        self.port, self.process, self.directory, self.log = port, process, directory, log
        self.general = general or {}

    def admin(self, password=None, dbname="pgcat", **kwargs):
        """A session on the admin console with the configured admin credentials."""
        return self.connect(dbname, self.general.get("admin_username"),
                            self.general.get("admin_password") if password is None else password, **kwargs)

    def connect(self, dbname, user, password, **kwargs):
        options = {"autocommit": True, "cursor_factory": psycopg.ClientCursor, "connect_timeout": 15,
                   "sslmode": "disable", **kwargs}
        return psycopg.connect(host="127.0.0.1", port=self.port, dbname=dbname, user=user, password=password, **options)

    def alpha(self, **kwargs):
        return self.connect("alpha", "app", "alpha-secret", **kwargs)

    def beta(self, **kwargs):
        return self.connect("beta", "app", "beta-secret", **kwargs)


@contextmanager
def run_pooler(general=None, pools=None, *, config_text=None, args=None, env=None, config_name="pools.toml"):
    """Start /app/program with a generated configuration and stop it afterwards."""
    port = free_port()
    general = {"host": "127.0.0.1", "port": port, "admin_username": "pool_admin",
               "admin_password": "admin-" + secrets.token_hex(4), **(general or {})}
    with workspace() as directory:
        config = directory / config_name
        write_fixture(config, config_text if config_text is not None else render_config(general, pools), mode=0o644)
        log = tempfile.TemporaryFile("w+")  # root-private; never a path inside the candidate's directory
        process = start_command(["/app/program", *(args if args is not None else [str(config)])], cwd=str(directory),
                                env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            if not wait_for_port(port, process):
                log.seek(0)
                pytest.fail(f"program did not listen on 127.0.0.1:{port} (exit {process.poll()}): {log.read()[-2000:]}")
            yield Pooler(port, process, directory, log, general)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            log.close()


@pytest.fixture
def pooler(tenants):
    """The standard two-tenant pooler: alpha (pool_size 2) and beta (pool_size 3), same frontend user `app`."""
    with run_pooler(pools=tenant_pools(tenants)) as p:
        yield p


@pytest.fixture
def replica_servers(tenants):
    """A primary on the first RDS instance and a replica on the second; markers read 'alpha' and 'alpha@2'."""
    tenants.provision(tenants.engines[1], "@2")
    first, second = tenants.engines
    return [[first.engine_host, first.engine_port, "primary"], [second.engine_host, second.engine_port, "replica"]]


def marker(conn):
    return conn.execute("SELECT tenant FROM marker").fetchone()[0]


def show_rows(conn, command):
    """(column names, rows) of a statement, rows sorted (admin listings come in no fixed order)."""
    cursor = conn.execute(command)
    names = [column.name for column in cursor.description] if cursor.description else None
    return names, sorted(cursor.fetchall(), key=repr) if cursor.description else None


def wait_until(predicate, timeout=10, interval=0.1):
    end = time.time() + timeout
    while True:
        value = predicate()
        if value or time.time() > end:
            return value
        time.sleep(interval)
