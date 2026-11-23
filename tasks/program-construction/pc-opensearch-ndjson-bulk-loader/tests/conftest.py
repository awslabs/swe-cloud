import json
import sys
import time
import urllib.request
import uuid
from pathlib import Path

import boto3
import pytest

sys.path.insert(0, str(Path(__file__).parent))

from harness import workspace  # noqa: E402
from support import http  # noqa: E402


def pytest_collection_modifyitems(items):
    """Domain setup counts toward the first test that needs it."""
    for item in items:
        if "server" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.timeout(600))
            break


@pytest.fixture(scope="session")
def server():
    client = boto3.client("opensearch")
    name = "bulk-" + uuid.uuid4().hex[:10]
    client.create_domain(
        DomainName=name,
        EngineVersion="OpenSearch_2.19",
        ClusterConfig={"InstanceType": "m5.large.search", "InstanceCount": 1},
    )
    try:
        deadline = time.time() + 540
        while True:
            status = client.describe_domain(DomainName=name)["DomainStatus"]
            if status.get("Endpoint") and not status.get("Processing"):
                break
            assert time.time() < deadline, status
            time.sleep(2)
        endpoint = status["Endpoint"]
        url = (endpoint if endpoint.startswith("http") else "http://" + endpoint).rstrip("/")
        while True:
            try:
                with urllib.request.urlopen(url + "/_cluster/health", timeout=5) as response:
                    if json.load(response).get("status") in ("green", "yellow"):
                        break
            except OSError:
                pass
            assert time.time() < deadline, "cluster never became healthy"
            time.sleep(2)
        yield url
    finally:
        client.delete_domain(DomainName=name)


@pytest.fixture
def new_index(server):
    """Factory of fresh index names, all deleted after the test."""
    names = []

    def make(prefix="t"):
        name = f"{prefix}-{uuid.uuid4().hex[:12]}"
        names.append(name)
        return name

    yield make
    for name in names:
        http(server, "DELETE", f"/{name}")


@pytest.fixture
def index(new_index):
    return new_index()


@pytest.fixture
def pipeline(server):
    names = []

    def make(processors):
        name = f"pl-{uuid.uuid4().hex[:12]}"
        status, body = http(server, "PUT", f"/_ingest/pipeline/{name}", {"processors": processors})
        assert status == 200, body
        names.append(name)
        return name

    yield make
    for name in names:
        http(server, "DELETE", f"/_ingest/pipeline/{name}")


@pytest.fixture
def root():
    with workspace() as path:
        yield path
