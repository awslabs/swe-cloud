"""Client TLS with a configured certificate, and the trust authentication type for users and the admin console."""

import struct
import subprocess

import psycopg
import pytest

from conftest import run_pooler, tenant_pools
from harness import workspace, write_fixture
from pgwire import Wire

pytestmark = pytest.mark.usefixtures("tenants")


@pytest.fixture
def certificate():
    """A self-signed certificate and key, readable by the program."""
    with workspace() as directory:
        key, cert = directory / "server.key.tmp", directory / "server.crt.tmp"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert),
                        "-days", "2", "-subj", "/CN=localhost"], check=True, capture_output=True, timeout=60)
        write_fixture(directory / "server.key", key.read_bytes(), mode=0o600)
        write_fixture(directory / "server.crt", cert.read_bytes(), mode=0o644)
        key.unlink()
        cert.unlink()
        yield {"tls_certificate": str(directory / "server.crt"), "tls_private_key": str(directory / "server.key")}


def test_tls_session_with_a_configured_certificate(tenants, certificate):
    with run_pooler(pools=tenant_pools(tenants), general=certificate) as pooler:
        with pooler.alpha(sslmode="require") as conn:
            assert conn.pgconn.ssl_in_use
            assert conn.execute("SELECT (SELECT tenant FROM marker), current_user::text").fetchall() == [
                ("alpha", tenants.alpha.owner)]


def test_tls_login_still_checks_the_password(tenants, certificate):
    with run_pooler(pools=tenant_pools(tenants), general=certificate) as pooler:
        with pytest.raises(psycopg.OperationalError):
            pooler.connect("alpha", "app", "wrong", sslmode="require")
        with pooler.beta(sslmode="require") as conn:
            assert conn.execute("SELECT (SELECT tenant FROM marker)").fetchall() == [("beta",)]


def test_plaintext_clients_are_still_served_with_tls_configured(tenants, certificate):
    with run_pooler(pools=tenant_pools(tenants), general=certificate) as pooler:
        with pooler.alpha() as conn:
            assert not conn.pgconn.ssl_in_use
            assert conn.execute("SELECT (SELECT tenant FROM marker)").fetchall() == [("alpha",)]


def test_tls_request_is_declined_without_a_certificate(tenants, pooler):
    with Wire(pooler.port) as wire:
        wire.sock.sendall(struct.pack("!ii", 8, 80877103))
        assert wire.sock.recv(1) == b"N"
        # The same connection continues with a plaintext startup.
        wire.startup(user="app", database="alpha")
        kind, body = wire.read()
        assert kind == b"R" and struct.unpack("!i", body[:4])[0] == 5


def test_trust_user_logs_in_without_a_password_exchange(tenants):
    pools = tenant_pools(tenants)
    pools["alpha"]["users"][0]["auth_type"] = "trust"
    with run_pooler(pools=pools) as pooler:
        with Wire(pooler.port) as wire:
            wire.startup(user="app", database="alpha")
            messages = wire.until_ready()
        assert messages[0] == (b"R", struct.pack("!i", 0))
        assert messages[-1] == (b"Z", b"I")
        with pooler.connect("alpha", "app", "anything") as conn:
            assert conn.execute("SELECT (SELECT tenant FROM marker)").fetchall() == [("alpha",)]
        # Other users keep their password exchange.
        with Wire(pooler.port) as wire:
            wire.startup(user="app", database="beta")
            kind, body = wire.read()
            assert kind == b"R" and struct.unpack("!i", body[:4])[0] == 5


def test_trust_admin_console_skips_the_password_exchange(tenants):
    with run_pooler(pools=tenant_pools(tenants), general={"admin_auth_type": "trust"}) as pooler:
        with Wire(pooler.port) as wire:
            wire.startup(user="anyone", database="pgcat")
            messages = wire.until_ready()
            assert messages[0] == (b"R", struct.pack("!i", 0))
            reply = wire.query("SHOW USERS")
            assert [kind for kind, _ in reply] == [b"T", b"D", b"D", b"C", b"Z"]
