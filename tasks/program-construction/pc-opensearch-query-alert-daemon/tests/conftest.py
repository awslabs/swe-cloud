import time

import boto3
import pytest

from alertkit import STATE_ALIAS, STATE_PATTERN, Cluster, Daemon, Topic, tag
from harness import workspace

# The daemon's own state template is rejected by this venue's OpenSearch (it carries an
# `index.lifecycle` setting), so the test environment provides the documented one.
VERIFIER_STATE_TEMPLATE = {
    "index_patterns": [STATE_PATTERN],
    "priority": 50,
    "template": {
        "aliases": {STATE_ALIAS: {}},
        "mappings": {
            "properties": {
                "@timestamp": {"type": "date"},
                "rule_name": {"type": "keyword"},
                "next_query": {"type": "date"},
                "hostname": {"type": "keyword"},
                "hits_count": {"type": "long"},
                "hits": {"type": "object", "enabled": False},
            }
        },
    },
}


def pytest_collection_modifyitems(items):
    """Domain creation counts toward the first test that needs it."""
    for item in items:
        if "cluster" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.timeout(600))
            break


@pytest.fixture(scope="session")
def cluster():
    client = boto3.client("opensearch")
    name = "qad-" + tag()
    template = "verifier-state-" + tag()
    url = None
    client.create_domain(
        DomainName=name,
        EngineVersion="OpenSearch_2.11",
        ClusterConfig={"InstanceType": "t3.small.search", "InstanceCount": 1},
    )
    try:
        deadline = time.monotonic() + 540
        while True:
            status = client.describe_domain(DomainName=name)["DomainStatus"]
            if status.get("Endpoint") and not status.get("Processing"):
                break
            assert time.monotonic() < deadline, "domain never became available"
            time.sleep(2)
        endpoint = status["Endpoint"]
        url = endpoint if endpoint.startswith("http") else "http://" + endpoint
        handle = Cluster(url)
        while True:
            try:
                if handle.http("GET", "/")[0] == 200:
                    break
            except OSError:
                pass
            assert time.monotonic() < deadline, "domain endpoint never answered"
            time.sleep(2)
        code, body = handle.http("PUT", f"/_index_template/{template}", VERIFIER_STATE_TEMPLATE)
        assert code == 200, (code, body)
        yield handle
    finally:
        if url:
            try:
                Cluster(url).http("DELETE", f"/_index_template/{template}")
            except Exception:
                pass
        client.delete_domain(DomainName=name)


@pytest.fixture
def root():
    with workspace() as path:
        yield path


@pytest.fixture
def indices(cluster):
    """Names of per-test indices; deleted after the test."""
    created = []
    yield created
    for name in created:
        cluster.delete_index(name)


@pytest.fixture
def daemon(root, cluster):
    instance = Daemon(root, cluster)
    yield instance
    instance.stop()


@pytest.fixture
def topic():
    instance = Topic()
    yield instance
    instance.close()


LOG_PROPERTIES = {
    "level": {"type": "keyword"},
    "service": {"type": "keyword"},
    "host": {"type": "keyword"},
    "latency": {"type": "long"},
    "ok": {"type": "boolean"},
    "msg": {"type": "text"},
    "tags": {"type": "keyword"},
}

# Adapted from the bucket/hit shapes in the upstream extraction and transform tests,
# seeded as real documents so every response comes from the cluster.
LOG_DOCS = [
    {"level": "error", "service": "api", "host": "h1", "latency": 120, "ok": False, "msg": "api timeout one", "meta": {"zone": "z1", "n": 1}, "tags": ["a", "b"]},
    {"level": "error", "service": "api", "host": "h2", "latency": 80, "ok": True, "msg": "api timeout two", "meta": {"zone": "z2"}, "tags": ["c"]},
    {"level": "error", "service": "billing", "host": "h1", "latency": 300, "ok": False, "msg": "billing failure", "tags": []},
    {"level": "info", "service": "api", "host": "h1", "latency": 5, "ok": True, "msg": "healthy", "tags": ["a"]},
    {"level": "warn", "service": "web", "host": "h3", "latency": 40, "ok": True, "msg": "slow", "tags": ["z"]},
]


@pytest.fixture
def logs(cluster, indices):
    """A fresh index holding LOG_DOCS with ids "0".."4"."""
    name = "logs-" + tag()
    indices.append(name)
    cluster.create_index(name, LOG_PROPERTIES, LOG_DOCS)
    return name
