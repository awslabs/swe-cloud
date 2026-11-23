import os
import socket
import time
import urllib.parse
import uuid

import boto3
import pytest

import support


def pytest_collection_modifyitems(items):
    # Engine provisioning happens inside the first test that needs it.
    for item in items:
        if "valkey" in item.fixturenames:
            item.add_marker(pytest.mark.timeout(600))
            break


def _endpoint(group):
    """The group's primary endpoint, reachable from this container."""
    ep = (group.get("NodeGroups") or [{}])[0].get("PrimaryEndpoint")
    if group.get("Status") != "available" or not ep:
        return None
    host = ep["Address"]
    if host in ("localhost", "127.0.0.1"):
        # The venue reports a loopback address for engine ports it serves on its own host.
        host = urllib.parse.urlparse(os.environ.get("AWS_ENDPOINT_URL", "http://engines:4566")).hostname
    try:
        socket.create_connection((host, ep["Port"]), 2).close()
    except OSError:
        return None
    return f"{host}:{ep['Port']}"


@pytest.fixture(scope="session")
def valkey():
    """Two ElastiCache Valkey replication groups: main storage and the per-second instance."""
    ec = boto3.client("elasticache")
    tag = uuid.uuid4().hex[:8]
    names = {"main": f"dlm{tag}", "persec": f"dls{tag}"}
    created = []
    try:
        for name in names.values():
            ec.create_replication_group(ReplicationGroupId=name, ReplicationGroupDescription="descriptor-limiter tests",
                                        Engine="valkey", CacheNodeType="cache.t4g.micro", NumCacheClusters=1)
            created.append(name)
        found = {}
        deadline = time.time() + 500
        while len(found) < len(names) and time.time() < deadline:
            for role, name in names.items():
                if role not in found:
                    group = ec.describe_replication_groups(ReplicationGroupId=name)["ReplicationGroups"][0]
                    address = _endpoint(group)
                    if address:
                        found[role] = address
            time.sleep(1)
        assert len(found) == len(names), f"replication groups never became available: {found}"
        yield found
    finally:
        for name in created:
            try:
                ec.delete_replication_group(ReplicationGroupId=name)
            except Exception:  # noqa: BLE001 -- best-effort teardown
                pass


@pytest.fixture
def domain(valkey):
    """A unique rate-limit domain; its counters are removed from both instances afterwards."""
    name = "t" + uuid.uuid4().hex[:12]
    yield name
    for address in valkey.values():
        support.delete_prefix(address, name)


@pytest.fixture
def key_prefixes(valkey):
    """Extra key prefixes (CACHE_KEY_PREFIX values) a test uses; cleaned on both instances."""
    prefixes = []
    yield prefixes
    for address in valkey.values():
        for prefix in prefixes:
            support.delete_prefix(address, prefix)


@pytest.fixture
def runtime():
    tree = support.Runtime()
    yield tree
    tree.cleanup()


@pytest.fixture
def launch(valkey, runtime):
    """Start /app/program against `runtime`; every server is stopped after the test."""
    servers = []

    def start(files=None, *, wait=True, **env):
        for relative, text in (files or {}).items():
            runtime.write(relative, text)
        server = support.Server(valkey, runtime, env)
        servers.append(server)
        return server.start() if wait else server

    yield start
    for server in servers:
        server.stop()
