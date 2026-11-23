"""Shared fixtures: one Valkey replication group per session, per-test key prefixes and
service processes started as the candidate."""

import socket
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field

import boto3
import pytest
import redis

from harness import start_command, workspace
from metrics import http_get, parse

PROGRAM = "/app/program"
DATABASES = 16


@dataclass
class Engine:
    host: str
    port: int

    @property
    def url(self):
        return f"redis://{self.host}:{self.port}"

    def db(self, number):
        return redis.Redis(host=self.host, port=self.port, db=number, socket_timeout=15)


def _endpoint(group):
    for node_group in group.get("NodeGroups") or []:
        endpoint = node_group.get("PrimaryEndpoint")
        if endpoint and endpoint.get("Address"):
            return endpoint
    endpoint = group.get("ConfigurationEndpoint")
    return endpoint if endpoint and endpoint.get("Address") else None


@pytest.fixture(scope="session")
def engine():
    client = boto3.client("elasticache")
    group_id = "kg-" + uuid.uuid4().hex[:12]
    client.create_replication_group(
        ReplicationGroupId=group_id,
        ReplicationGroupDescription="keyspace-gauges verifier",
        Engine="valkey",
        CacheNodeType="cache.t3.micro",
        NumCacheClusters=1,
    )
    try:
        deadline = time.monotonic() + 540
        while True:
            group = client.describe_replication_groups(ReplicationGroupId=group_id)["ReplicationGroups"][0]
            endpoint = _endpoint(group)
            if group.get("Status") == "available" and endpoint:
                break
            assert time.monotonic() < deadline, f"replication group never became available: {group}"
            time.sleep(2)
        found = Engine(endpoint["Address"], int(endpoint["Port"]))
        while True:
            try:
                found.db(0).ping()
                break
            except redis.RedisError:
                assert time.monotonic() < deadline, "engine never answered PING"
                time.sleep(1)
        yield found
    finally:
        try:
            client.delete_replication_group(ReplicationGroupId=group_id)
        except Exception:  # noqa: BLE001 -- teardown is best effort
            pass


def pytest_collection_modifyitems(items):
    """The first engine-backed test pays for provisioning the replication group."""
    for item in items:
        if "engine" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.timeout(600))
            break


@dataclass
class Keyspace:
    engine: Engine
    prefix: str
    reserved: list = field(default_factory=list)

    def db(self, number=0):
        return self.engine.db(number)

    def reserve(self, number):
        """A database used by only this test (key groups scan every database)."""
        self.db(number).flushdb()
        self.reserved.append(number)
        return self.db(number)

    def cleanup(self):
        for number in range(DATABASES):
            client = self.db(number)
            if number in self.reserved:
                client.flushdb()
                continue
            keys = list(client.scan_iter(match=self.prefix + "*", count=1000))
            if keys:
                client.delete(*keys)


@pytest.fixture
def keyspace(engine):
    space = Keyspace(engine, "kg" + uuid.uuid4().hex[:10])
    try:
        yield space
    finally:
        space.cleanup()


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Service:
    def __init__(self, args, env=None, cwd=None):
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.log = tempfile.TemporaryFile()
        self.process = start_command(
            [PROGRAM, "-web.listen-address", f"127.0.0.1:{self.port}", *map(str, args)],
            cwd=cwd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=self.log,
        )

    def wait_ready(self):
        deadline = time.monotonic() + 20
        while True:
            if self.process.poll() is not None:
                raise AssertionError(f"service exited with {self.process.returncode}: {self.stderr()}")
            try:
                status, _ = http_get(self.base, "/health", timeout=5)
                if status == 200:
                    return
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise AssertionError(f"service never answered /health: {self.stderr()}")
            time.sleep(0.2)

    def get(self, path):
        return http_get(self.base, path)

    def samples(self, path="/metrics", namespace="redis"):
        status, body = self.get(path)
        assert status == 200, f"GET {path} returned {status}: {body[:500]}"
        return parse(body, namespace)

    def stderr(self):
        self.log.seek(0)
        return self.log.read().decode("utf-8", "replace")[-2000:]

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.log.close()


@pytest.fixture
def service():
    """Start the service with given flags: service(*args, env=None, cwd=None)."""
    started = []

    def start(*args, env=None, cwd=None):
        running = Service(args, env=env, cwd=cwd)
        started.append(running)
        running.wait_ready()
        return running

    try:
        yield start
    finally:
        for running in started:
            running.stop()


@pytest.fixture
def scratch():
    with workspace() as directory:
        yield directory
