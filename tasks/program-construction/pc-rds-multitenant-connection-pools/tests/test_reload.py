"""Configuration reload: the admin RELOAD command, SIGHUP and periodic autoreload."""

import signal
import socket

import psycopg
import pytest

from conftest import free_port, render_config, run_pooler, show_rows, tenant_pools, wait_until
from harness import write_fixture
from pgwire import refusal

pytestmark = pytest.mark.usefixtures("tenants")


def _rewrite(pooler, pools, **general):
    write_fixture(pooler.directory / "pools.toml", render_config({**pooler.general, **general}, pools), mode=0o644)


def _reload(pooler):
    with pooler.admin() as admin:
        assert admin.execute("RELOAD").statusmessage == "RELOAD"


def test_reload_applies_a_changed_password(tenants):
    pools = tenant_pools(tenants)
    with run_pooler(pools=pools) as pooler:
        pools["alpha"]["users"][0]["password"] = "alpha-rotated"
        _rewrite(pooler, pools)
        _reload(pooler)
        assert refusal(pooler.port, "alpha", "app", "alpha-secret")[:2] == ([5], [("FATAL", "28P01")])
        with pooler.connect("alpha", "app", "alpha-rotated") as conn:
            assert conn.execute("SELECT (SELECT tenant FROM marker)").fetchall() == [("alpha",)]


def test_reload_adds_a_new_pool(tenants):
    pools = tenant_pools(tenants)
    with run_pooler(pools=pools) as pooler:
        assert refusal(pooler.port, "gamma", "app", "gamma-secret")[:2] == ([], [("FATAL", "58000")])
        pools["gamma"] = {**pools["beta"], "users": [{**pools["beta"]["users"][0], "password": "gamma-secret"}]}
        _rewrite(pooler, pools)
        _reload(pooler)
        with pooler.connect("gamma", "app", "gamma-secret") as conn:
            assert conn.execute("SELECT current_database()::text").fetchall() == [(tenants.beta.database,)]


def test_sighup_reloads_and_drops_a_removed_pool(tenants):
    pools = tenant_pools(tenants)
    with run_pooler(pools=pools) as pooler:
        with pooler.beta() as conn:
            conn.execute("SELECT 1")
        del pools["beta"]
        _rewrite(pooler, pools)
        pooler.process.send_signal(signal.SIGHUP)
        assert wait_until(lambda: refusal(pooler.port, "beta", "app", "beta-secret")[:2] == ([], [("FATAL", "58000")]),
                          timeout=10)
        with pooler.alpha() as conn:
            assert conn.execute("SELECT (SELECT tenant FROM marker)").fetchall() == [("alpha",)]


def test_reload_applies_a_new_pool_size(tenants):
    pools = tenant_pools(tenants)
    with run_pooler(pools=pools) as pooler:
        pools["beta"]["users"][0]["pool_size"] = 1
        _rewrite(pooler, pools)
        _reload(pooler)
        with pooler.admin() as admin:
            sizes = {row[0]: row[5] for row in show_rows(admin, "SHOW DATABASES")[1]}
        assert sizes == {"alpha_shard_0_primary": 2, "beta_shard_0_primary": 1}
        with pooler.beta() as holder, pooler.beta() as waiter:
            holder.execute("BEGIN")
            holder.execute("SELECT 1")
            with pytest.raises(psycopg.Error) as failed:
                waiter.execute("SELECT 2")
            assert failed.value.sqlstate == "58000"


def test_reload_keeps_connected_clients_serving(tenants):
    pools = tenant_pools(tenants)
    with run_pooler(pools=pools) as pooler:
        conn = pooler.alpha()
        try:
            conn.execute("SELECT 1")
            pools["alpha"]["users"][0]["password"] = "alpha-rotated"
            _rewrite(pooler, pools)
            _reload(pooler)
            # Already authenticated sessions are not logged out.
            assert conn.execute("SELECT (SELECT tenant FROM marker)").fetchall() == [("alpha",)]
        finally:
            conn.close()


def test_reload_of_an_unreadable_file_keeps_the_old_configuration(tenants):
    with run_pooler(pools=tenant_pools(tenants)) as pooler:
        write_fixture(pooler.directory / "pools.toml", "[general\nport = = 1\n", mode=0o644)
        with pooler.admin() as admin:
            with pytest.raises(psycopg.Error):
                admin.execute("RELOAD")
        assert pooler.process.poll() is None
        with pooler.alpha() as conn:
            assert conn.execute("SELECT (SELECT tenant FROM marker)").fetchall() == [("alpha",)]


def test_reload_does_not_move_the_listening_port(tenants):
    pools = tenant_pools(tenants)
    with run_pooler(pools=pools) as pooler:
        moved = free_port()
        _rewrite(pooler, pools, port=moved)
        _reload(pooler)
        with pooler.beta() as conn:
            assert conn.execute("SELECT (SELECT tenant FROM marker)").fetchall() == [("beta",)]
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", moved), 1).close()


def test_autoreload_picks_up_a_changed_file(tenants):
    pools = tenant_pools(tenants)
    with run_pooler(pools=pools, general={"autoreload": 500}) as pooler:
        pools["beta"]["users"][0]["password"] = "beta-rotated"
        _rewrite(pooler, pools)

        def rotated():
            try:
                with pooler.connect("beta", "app", "beta-rotated") as conn:
                    return conn.execute("SELECT (SELECT tenant FROM marker)").fetchall() == [("beta",)]
            except psycopg.OperationalError:
                return False

        assert wait_until(rotated, timeout=10, interval=0.3)
