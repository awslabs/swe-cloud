"""Shared fixtures: one Valkey cache per session, one gateway process per configuration."""

import json
import os
import socket
import time
import urllib.parse
import uuid

import boto3
import pytest
import redis

from gw import free_port
from harness import start_command, workspace


@pytest.fixture(scope="session")
def valkey():
    """A Valkey replication group; yields the (host, port) the program must use."""
    ec = boto3.client("elasticache")
    group = "gw-" + uuid.uuid4().hex[:10]
    ec.create_replication_group(
        ReplicationGroupId=group,
        ReplicationGroupDescription="gateway verifier",
        Engine="valkey",
        CacheNodeType="cache.t4g.micro",
        NumCacheClusters=1,
    )
    try:
        deadline = time.time() + 500
        while True:
            described = ec.describe_replication_groups(ReplicationGroupId=group)["ReplicationGroups"][0]
            if described["Status"] == "available":
                break
            assert time.time() < deadline, "replication group never became available"
            time.sleep(2)
        endpoint = described["NodeGroups"][0]["PrimaryEndpoint"]
        host, port = endpoint["Address"], int(endpoint["Port"])
        if host in ("localhost", "127.0.0.1", "::1"):
            host = urllib.parse.urlparse(os.environ["AWS_ENDPOINT_URL"]).hostname
        deadline = time.time() + 60
        while True:
            try:
                if redis.Redis(host=host, port=port, socket_timeout=5).ping():
                    break
            except redis.RedisError:
                assert time.time() < deadline, "Valkey endpoint never answered"
                time.sleep(1)
        yield host, port
    finally:
        try:
            ec.delete_replication_group(ReplicationGroupId=group)
        except Exception:  # noqa: BLE001 -- teardown must not mask test results
            pass


@pytest.fixture
def db(valkey):
    """db(n) returns a redis-py client for database n of the shared cache."""
    clients = {}

    def client(number=0):
        if number not in clients:
            clients[number] = redis.Redis(host=valkey[0], port=valkey[1], db=number, socket_timeout=10)
        return clients[number]

    yield client
    for c in clients.values():
        c.close()


@pytest.fixture
def key(valkey):
    """A unique key prefix; every key under it is removed from all databases afterwards."""
    prefix = "t" + uuid.uuid4().hex[:12]
    yield prefix
    for number in range(16):
        c = redis.Redis(host=valkey[0], port=valkey[1], db=number, socket_timeout=10)
        try:
            found = list(c.scan_iter(prefix + "*"))
            if found:
                c.delete(*found)
        finally:
            c.close()


class Gateway:
    def __init__(self, process, port, directory):
        self.process, self.port, self.directory = process, port, directory

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(5)
            except Exception:  # noqa: BLE001
                self.process.kill()
                self.process.wait(5)


@pytest.fixture
def gateway(valkey):
    """gateway(**config) starts the program with a webdis.json built from defaults plus overrides."""
    started = []
    spaces = []

    def start(env=None, connect_port=None, **overrides):
        port = connect_port or free_port()
        config = {
            "redis_host": valkey[0],
            "redis_port": valkey[1],
            "http_host": "127.0.0.1",
            "http_port": port,
            "threads": 2,
            "pool_size": 2,
            "daemonize": False,
            "websockets": True,
            "database": 0,
            "verbosity": 0,
            "logfile": "webdis.log",
        }
        config.update(overrides)
        context = workspace()
        directory = context.__enter__()
        spaces.append(context)
        path = directory / "webdis.json"
        path.write_text(json.dumps(config))
        path.chmod(0o644)
        process = start_command(["/app/program", "webdis.json"], cwd=directory, env=env,
                                stdout=open(os.devnull, "wb"), stderr=open(os.devnull, "wb"))
        g = Gateway(process, port, directory)
        started.append(g)
        deadline = time.time() + 15
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                assert process.poll() is None, f"program exited early with status {process.returncode}"
                assert time.time() < deadline, "program never listened on its http_port"
                time.sleep(0.1)
        return g

    yield start
    for g in started:
        g.stop()
    for context in spaces:
        context.__exit__(None, None, None)
