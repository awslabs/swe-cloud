"""Configurations the program refuses at startup: it exits with the configuration status and never listens."""

import socket

import pytest

from conftest import free_port, render_config, tenant_pools
from harness import run_command, workspace, write_fixture

pytestmark = pytest.mark.usefixtures("tenants")


def refused(pools):
    port = free_port()
    general = {"host": "127.0.0.1", "port": port, "admin_username": "pool_admin", "admin_password": "admin-pw"}
    with workspace() as directory:
        config = directory / "pools.toml"
        write_fixture(config, render_config(general, pools), mode=0o644)
        result = run_command(["/app/program", str(config)], cwd=str(directory), capture_output=True, timeout=20)
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), 1).close()
    return result.returncode


def primary(tenants):
    engine = tenants.engines[0]
    return [engine.engine_host, engine.engine_port, "primary"]


def test_unknown_default_role_is_refused(tenants):
    assert refused(tenant_pools(tenants, alpha_settings={"default_role": "writer"})) == 78


def test_two_primaries_in_a_shard_are_refused(tenants):
    second = ["127.0.0.2", tenants.engines[0].engine_port, "primary"]
    assert refused(tenant_pools(tenants, alpha_servers=[primary(tenants), second])) == 78


def test_duplicate_servers_in_a_shard_are_refused(tenants):
    assert refused(tenant_pools(tenants, alpha_servers=[primary(tenants), primary(tenants)])) == 78


def test_unknown_server_role_is_refused(tenants):
    engine = tenants.engines[0]
    assert refused(tenant_pools(tenants, alpha_servers=[[engine.engine_host, engine.engine_port, "leader"]])) == 78


def test_shard_without_servers_is_refused(tenants):
    pools = tenant_pools(tenants)
    pools["alpha"]["servers"] = []
    assert refused(pools) == 78


def test_unknown_pool_mode_is_refused(tenants):
    assert refused(tenant_pools(tenants, alpha_settings={"pool_mode": "statement"})) == 78


def test_read_write_splitting_without_the_query_parser_is_refused(tenants):
    assert refused(tenant_pools(tenants, alpha_settings={"query_parser_read_write_splitting": True})) == 78


def test_invalid_sharding_key_regex_is_refused(tenants):
    assert refused(tenant_pools(tenants, alpha_settings={"sharding_key_regex": "(("})) == 78


def test_default_shard_beyond_the_shards_is_refused(tenants):
    assert refused(tenant_pools(tenants, alpha_settings={"default_shard": "shard_3"})) == 78


def test_unknown_sharding_function_is_refused(tenants):
    assert refused(tenant_pools(tenants, alpha_settings={"sharding_function": "md5"})) == 78


def test_min_pool_size_above_pool_size_is_refused(tenants):
    pools = tenant_pools(tenants)
    pools["alpha"]["users"][0]["min_pool_size"] = 5
    assert refused(pools) == 78


def test_user_without_a_password_is_refused(tenants):
    pools = tenant_pools(tenants)
    del pools["alpha"]["users"][0]["password"]
    assert refused(pools) == 78
