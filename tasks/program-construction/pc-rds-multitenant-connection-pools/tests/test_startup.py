"""Invocation and connection startup: configuration sources, MD5 challenge, pool selection and refusals."""

import socket
import struct
import subprocess

import psycopg
import pytest

from conftest import free_port, render_config, run_pooler, tenant_pools, wait_for_port
from harness import run_command, start_command, workspace, write_fixture
from pgwire import Wire, error_fields, md5_response, refusal


def _general(port):
    return {"host": "127.0.0.1", "port": port, "admin_username": "pool_admin", "admin_password": "admin-pw"}


WRONG_PASSWORD = ([5], [("FATAL", "28P01")])
NO_POOL = ([], [("FATAL", "58000")])


def _refusal(pooler, alias, user, password):
    """Authentication requests and errors a raw client sees; also checks psycopg is refused."""
    codes, errors, kinds = refusal(pooler.port, alias, user, password)
    assert b"Z" not in kinds or b"E" in kinds
    assert not any(kind in (b"T", b"D", b"K") for kind in kinds)
    with pytest.raises(psycopg.OperationalError):
        pooler.connect(alias, user, password)
    return codes, errors


def _beta_identity(port):
    with psycopg.connect(host="127.0.0.1", port=port, dbname="beta", user="app", password="beta-secret",
                         autocommit=True, cursor_factory=psycopg.ClientCursor, sslmode="disable") as conn:
        return conn.execute("SELECT current_database()::text, (SELECT tenant FROM marker)").fetchall()


@pytest.mark.parametrize("source", ["argument", "environment", "working-directory", "logging-options"])
def test_configuration_sources(tenants, source):
    port = free_port()
    with workspace() as directory:
        name = "pgcat.toml" if source == "working-directory" else "elsewhere.toml"
        config = directory / name
        write_fixture(config, render_config(_general(port), tenant_pools(tenants)), mode=0o644)
        args = {"argument": ["/app/program", str(config)],
                "logging-options": ["/app/program", "-l", "debug", "-F", "structured", "-n", str(config)]}.get(
                    source, ["/app/program"])
        env = {"CONFIG_FILE": str(config)} if source == "environment" else None
        process = start_command(args, cwd=str(directory), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            assert wait_for_port(port, process)
            assert _beta_identity(port) == [(tenants.beta.database, "beta")]
        finally:
            process.terminate()
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def _bad_config(tenants, port, case):
    general = _general(port)
    if case == "missing-admin-username":
        del general["admin_username"]
        return render_config(general, tenant_pools(tenants))
    if case == "not-toml":
        return "[general\nport = = 1\n"
    raise ValueError(case)


@pytest.mark.parametrize("case", ["missing-admin-username", "not-toml", "missing-file"])
def test_unusable_configuration_exits_with_config_status(tenants, case):
    port = free_port()
    with workspace() as directory:
        config = directory / "pools.toml"
        if case != "missing-file":
            write_fixture(config, _bad_config(tenants, port, case), mode=0o644)
        result = run_command(["/app/program", str(config)], cwd=str(directory), capture_output=True, timeout=20)
        assert result.returncode == 78
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", port), 1).close()


def test_exits_cleanly_on_sigterm(tenants):
    with run_pooler(pools=tenant_pools(tenants)) as pooler:
        with pooler.alpha() as conn:
            assert conn.execute("SELECT 1").fetchall() == [(1,)]
        pooler.process.terminate()
        assert pooler.process.wait(15) == 0


def test_startup_reports_server_parameters(tenants, pooler):
    with pooler.alpha(application_name="ledger-ui") as conn:
        server_version = conn.execute("SHOW server_version").fetchone()[0]
        assert conn.info.parameter_status("server_version") == server_version
        assert conn.info.parameter_status("client_encoding") == "UTF8"
        assert conn.execute("SHOW application_name").fetchall() == [("ledger-ui",)]
        assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE


def test_md5_challenge_and_simple_query_on_the_wire(tenants, pooler):
    with Wire(pooler.port) as wire:
        wire.startup(user="app", database="alpha")
        kind, body = wire.read()
        assert kind == b"R" and len(body) == 8 and struct.unpack("!i", body[:4])[0] == 5
        wire.send(b"p", md5_response("app", "alpha-secret", body[4:8]))
        messages = wire.until_ready()
        assert messages[0] == (b"R", struct.pack("!i", 0))
        kinds = [message[0] for message in messages]
        assert kinds[-1] == b"Z" and messages[-1][1] == b"I"
        assert set(kinds[1:-1]) <= {b"S", b"K", b"N"} and kinds.count(b"K") == 1
        reply = wire.query("SELECT current_database()::text AS db; SELECT 7 AS n")
        kinds = [message[0] for message in reply]
        assert kinds == [b"T", b"D", b"C", b"T", b"D", b"C", b"Z"]
        assert reply[1][1] == struct.pack("!hi", 1, len(tenants.alpha.database)) + tenants.alpha.database.encode()
        assert reply[2][1] == b"SELECT 1\0"
        assert reply[4][1] == struct.pack("!hi", 1, 1) + b"7"
        assert reply[-1][1] == b"I"
        wire.send(b"X")
        assert wire.until_closed()[-1] is None


def test_fresh_salt_for_each_connection(tenants, pooler):
    salts = []
    for _ in range(3):
        with Wire(pooler.port) as wire:
            wire.startup(user="app", database="alpha")
            kind, body = wire.read()
            assert kind == b"R" and struct.unpack("!i", body[:4])[0] == 5
            salts.append(body[4:8])
    assert len(set(salts)) == 3


@pytest.mark.parametrize(("alias", "user", "password"), [
    ("nosuchdb", "app", "alpha-secret"),
    ("alpha", "nobody", "alpha-secret"),
    ("ALPHA", "app", "alpha-secret"),
    ("alpha", "APP", "alpha-secret"),
])
def test_unknown_alias_or_user_is_refused_without_challenge(tenants, pooler, alias, user, password):
    before = tenants.activity()
    assert _refusal(pooler, alias, user, password) == NO_POOL
    assert tenants.activity() == before


@pytest.mark.parametrize("password", ["wrong", "alpha-secret ", "Alpha-secret", "a"])
def test_wrong_password_is_refused(tenants, pooler, password):
    before = tenants.activity()
    assert _refusal(pooler, "alpha", "app", password) == WRONG_PASSWORD
    assert tenants.activity() == before


def test_cleartext_password_message_is_refused(tenants, pooler):
    with Wire(pooler.port) as wire:
        wire.startup(user="app", database="alpha")
        kind, body = wire.read()
        assert kind == b"R" and struct.unpack("!i", body[:4])[0] == 5
        wire.send(b"p", b"alpha-secret\0")
        messages = [m for m in wire.until_closed() if isinstance(m, tuple)]
        assert [(error_fields(b)["V"], error_fields(b)["C"]) for k, b in messages if k == b"E"] == [("FATAL", "28P01")]
        assert not any(k == b"R" for k, _ in messages)


def test_backend_credentials_are_not_frontend_credentials(tenants, pooler):
    # The physical database name and the backend role are not pool names / frontend users.
    assert _refusal(pooler, tenants.alpha.database, "app", "alpha-secret") == NO_POOL
    assert _refusal(pooler, "alpha", tenants.alpha.owner, tenants.alpha.owner_password) == NO_POOL
    with pooler.alpha() as conn:
        assert conn.execute("SELECT current_user::text").fetchall() == [(tenants.alpha.owner,)]


def test_startup_without_database_uses_user_name_as_pool(tenants, pooler):
    # Without a database parameter the pool name is the user name; no pool "app" exists here.
    before = tenants.activity()
    with Wire(pooler.port) as wire:
        wire.startup(user="app")
        messages = [m for m in wire.until_closed() if isinstance(m, tuple)]
    assert not any(kind == b"R" for kind, _ in messages)
    errors = [error_fields(body) for kind, body in messages if kind == b"E"]
    assert [(e["V"], e["C"]) for e in errors] == [("FATAL", "58000")]
    assert tenants.activity() == before
