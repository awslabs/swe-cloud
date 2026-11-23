"""Shared fixtures: one Valkey replication group per session, a fresh cache namespace per test."""

import os
import time
import urllib.parse
import uuid

import boto3
import botocore.config
import botocore.exceptions
import pytest
import redis
from buildcache import Cache
from harness import workspace

ENGINE_TIMEOUT = 600


def pytest_collection_modifyitems(items):
    """Engine provisioning counts toward the first test that requests it."""
    for item in items:
        if "valkey" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.timeout(ENGINE_TIMEOUT))
            break


@pytest.fixture(scope="session")
def valkey():
    # Creating the first engine can block the emulator for a while; a timed-out request retried by
    # botocore then reports our own (uniquely named) group as already being created.
    elasticache = boto3.client("elasticache", config=botocore.config.Config(read_timeout=300, retries={"max_attempts": 5}))
    group = "bc-" + uuid.uuid4().hex[:12]
    try:
        elasticache.create_replication_group(
            ReplicationGroupId=group,
            ReplicationGroupDescription="build-cache verifier",
            Engine="valkey",
            CacheNodeType="cache.t3.micro",
        )
    except botocore.exceptions.ClientError as error:
        if "AlreadyExists" not in error.response.get("Error", {}).get("Code", ""):
            raise
    try:
        deadline = time.time() + ENGINE_TIMEOUT - 60
        while True:
            try:
                described = elasticache.describe_replication_groups(ReplicationGroupId=group)["ReplicationGroups"][0]
            except botocore.exceptions.ClientError as error:
                if "NotFound" not in error.response.get("Error", {}).get("Code", ""):
                    raise
                described = {"Status": "not yet visible"}
            if described.get("Status") == "available":
                break
            assert time.time() < deadline, f"replication group never became available: {described.get('Status')}"
            time.sleep(2)
        endpoint = described["NodeGroups"][0]["PrimaryEndpoint"]
        host, port = endpoint["Address"], int(endpoint["Port"])
        if host in ("localhost", "127.0.0.1"):
            host = urllib.parse.urlsplit(os.environ["AWS_ENDPOINT_URL"]).hostname
        client = redis.Redis(host=host, port=port, socket_connect_timeout=5, socket_timeout=5)
        while True:
            try:
                if client.ping():
                    break
            except redis.RedisError:
                pass
            assert time.time() < deadline, "valkey endpoint never answered PING"
            time.sleep(1)
        yield {"host": host, "port": port}
    finally:
        try:
            elasticache.delete_replication_group(ReplicationGroupId=group)
        except botocore.exceptions.ClientError:
            pass


@pytest.fixture
def cache(valkey):
    with workspace() as root:
        namespace = Cache(root, valkey)
        try:
            yield namespace
        finally:
            namespace.cleanup()
