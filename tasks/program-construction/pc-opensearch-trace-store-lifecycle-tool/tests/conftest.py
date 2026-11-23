import time
import urllib.request
import uuid

import boto3
import pytest

from support import Cluster, unique_prefix


@pytest.fixture(scope="session")
def cluster():
    """One OpenSearch domain for the session; tests isolate themselves by index prefix."""
    client = boto3.client("opensearch")
    name = "til-" + uuid.uuid4().hex[:10]
    client.create_domain(DomainName=name)
    try:
        deadline = time.time() + 540
        while True:
            status = client.describe_domain(DomainName=name)["DomainStatus"]
            if status.get("Endpoint") and not status.get("Processing"):
                break
            assert time.time() < deadline, f"domain {name} never became available: {status}"
            time.sleep(2)
        endpoint = status["Endpoint"]
        url = (endpoint if endpoint.startswith("http") else "http://" + endpoint).rstrip("/")
        while True:
            try:
                with urllib.request.urlopen(url, timeout=5) as resp:
                    if resp.status == 200:
                        break
            except OSError:
                pass
            assert time.time() < deadline, f"engine at {url} never answered"
            time.sleep(1)
        yield Cluster(url)
    finally:
        client.delete_domain(DomainName=name)


@pytest.fixture
def prefix(cluster):
    """A fresh index prefix (without trailing dash); everything named with it is deleted afterwards."""
    base = unique_prefix()
    try:
        yield base
    finally:
        cluster.cleanup(base)
