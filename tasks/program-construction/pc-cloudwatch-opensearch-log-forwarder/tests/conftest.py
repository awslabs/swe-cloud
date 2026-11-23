import time
import urllib.request
import uuid

import boto3
import pytest

from forwarder import Case, Search
from harness import workspace


def pytest_collection_modifyitems(items):
    # The shared OpenSearch engine starts inside the first test that needs it.
    for item in items:
        if "opensearch" in getattr(item, "fixturenames", ()) or "case" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.timeout(600))
            break


@pytest.fixture(scope="session")
def opensearch():
    client = boto3.client("opensearch")
    name = "fwd-" + uuid.uuid4().hex[:8]
    client.create_domain(
        DomainName=name,
        EngineVersion="OpenSearch_2.11",
        ClusterConfig={"InstanceType": "t3.small.search", "InstanceCount": 1},
        EBSOptions={"EBSEnabled": True, "VolumeType": "gp2", "VolumeSize": 10},
    )
    try:
        deadline = time.monotonic() + 540
        while True:
            status = client.describe_domain(DomainName=name)["DomainStatus"]
            endpoint = status.get("Endpoint")
            if endpoint and not status.get("Processing", False):
                endpoint = (endpoint if "://" in endpoint else "http://" + endpoint).rstrip("/")
                try:
                    with urllib.request.urlopen(endpoint + "/", timeout=5):
                        break
                except OSError:
                    pass
            assert time.monotonic() < deadline, f"OpenSearch domain never became available: {status}"
            time.sleep(2)
        search = Search(endpoint)
        # The disk monitor would otherwise lift index read-only-allow-delete blocks (test_retry.py) within ~30 s.
        status, body = search.request("PUT", "/_cluster/settings", {"persistent": {"cluster.routing.allocation.disk.threshold_enabled": False}})
        assert status == 200, body
        yield search
    finally:
        client.delete_domain(DomainName=name)


@pytest.fixture
def case(opensearch):
    with workspace() as root:
        current = Case(root, opensearch)
        try:
            yield current
        finally:
            current.cleanup()
