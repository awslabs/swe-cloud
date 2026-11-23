"""Intake to storage: span and service documents written through the write aliases."""

import json
import time
from pathlib import Path

import pytest

from sample_traces import TA, rich_payload
from support import BASE, ok, otlp_span, payload, resource_spans, rollover_tool, stored_doc, trace_server, wait_docs

EXPECTED = json.loads((Path(__file__).parent / "expected" / "rich_trace.json").read_text())
RICH_SERVICES = sorted([("alpha", op) for op in ("GET /cart", "SELECT", "compute", "consume", "publish", "unspecified")]
                       + [("beta", "GET /cart"), ("beta", "batch"),
                          ("missing-service-name", "lonely"), ("missing-service-name", "orphan")])


def init(cluster, prefix):
    ok(rollover_tool("init", cluster.url, "--index-prefix", prefix, "--shards", "1", "--replicas", "0"))


def service_pairs(hits):
    for h in hits:
        assert set(h["_source"]) == {"serviceName", "operationName"}, h["_source"]
    return sorted((h["_source"]["serviceName"], h["_source"]["operationName"]) for h in hits)


@pytest.mark.timeout(600)
def test_span_documents(cluster, prefix):
    p = prefix + "-"
    init(cluster, prefix)
    with trace_server(cluster.url, prefix) as server:
        server.ingest(rich_payload())
        hits = wait_docs(cluster, p, f"{p}jaeger-span-read", 10)
        time.sleep(1)
        hits = cluster.search(f"{p}jaeger-span-*")
    assert {h["_index"] for h in hits} == {f"{p}jaeger-span-000001"}
    actual = {h["_source"]["spanID"]: stored_doc(h["_source"]) for h in hits}
    assert len(actual) == len(hits) == 10
    expected = {d["spanID"]: stored_doc(d) for d in EXPECTED["span docs"]}
    assert sorted(actual) == sorted(expected)
    for span_id in expected:
        assert actual[span_id] == expected[span_id], span_id


def test_service_documents_are_unique_pairs(cluster, prefix):
    p = prefix + "-"
    init(cluster, prefix)
    with trace_server(cluster.url, prefix) as server:
        server.ingest(rich_payload())
        wait_docs(cluster, p, f"{p}jaeger-service-read", 10)
        # A second batch repeating known pairs plus one new pair.
        server.ingest(payload(resource_spans("alpha", [otlp_span("ab" * 16, f"{i:016x}", "SELECT", BASE + i, 10) for i in range(1, 4)]
                                             + [otlp_span("ab" * 16, "0000000000000009", "fresh", BASE, 10)])))
        wait_docs(cluster, p, f"{p}jaeger-span-read", 14)
        wait_docs(cluster, p, f"{p}jaeger-service-read", 11)
        time.sleep(1.5)
        cluster.refresh(p)
        hits = cluster.search(f"{p}jaeger-service-*")
    assert {h["_index"] for h in hits} == {f"{p}jaeger-service-000001"}
    assert service_pairs(hits) == sorted(RICH_SERVICES + [("alpha", "fresh")])
    assert cluster.search(f"{p}jaeger-dependencies-*") == []


def test_resending_identical_spans_stores_them_once(cluster, prefix):
    p = prefix + "-"
    init(cluster, prefix)
    with trace_server(cluster.url, prefix) as server:
        server.ingest(rich_payload())
        wait_docs(cluster, p, f"{p}jaeger-span-read", 10)
        server.ingest(rich_payload())
        marker = payload(resource_spans("marker", [otlp_span("cd" * 16, "00000000000000aa", "after", BASE, 10)]))
        server.ingest(marker)
        wait_docs(cluster, p, f"{p}jaeger-span-read", 11)
        time.sleep(1.5)
        cluster.refresh(p)
        spans = cluster.search(f"{p}jaeger-span-*")
        status, body = server.get(f"/api/traces/{TA}")
    assert sorted(h["_source"]["spanID"] for h in spans) == sorted([d["spanID"] for d in EXPECTED["span docs"]] + ["00000000000000aa"])
    assert status == 200 and len(body["data"][0]["spans"]) == 7


def test_malformed_and_empty_intake(cluster, prefix):
    p = prefix + "-"
    init(cluster, prefix)
    with trace_server(cluster.url, prefix) as server:
        status, _ = server.post_raw(b"{not json")
        assert status == 400
        status, _ = server.post_traces({"resourceSpans": []})
        assert status == 200
        server.ingest(payload(resource_spans("marker", [otlp_span("cd" * 16, "00000000000000aa", "after", BASE, 10)])))
        hits = wait_docs(cluster, p, f"{p}jaeger-span-read", 1)
        time.sleep(1)
        cluster.refresh(p)
        assert [h["_source"]["spanID"] for h in cluster.search(f"{p}jaeger-span-*")] == ["00000000000000aa"]
        status, body = server.get("/api/v3/services")
        assert status == 200 and body == {"services": ["marker"]}
