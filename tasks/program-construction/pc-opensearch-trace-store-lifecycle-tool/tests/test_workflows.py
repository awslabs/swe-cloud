"""Multi-step interactions: actions after one another and against a running trace server."""

import time

import pytest

from support import (BASE, DAY, FAMILIES, assert_template, expected_template, failed, family_layout, ok, otlp_span,
                     payload, resource_spans, rollover_tool, span_ids, trace_ids, trace_server, wait_docs, wait_until)


def init(cluster, prefix, *args):
    return ok(rollover_tool("init", cluster.url, "--index-prefix", prefix, "--shards", "1", "--replicas", "0", *args))


def roll(cluster, prefix, conditions="{}", *args):
    return ok(rollover_tool("rollover", cluster.url, "--index-prefix", prefix, "--conditions", conditions, *args))


def lookback_seconds(cluster, prefix):
    return ok(rollover_tool("lookback", cluster.url, "--index-prefix", prefix, "--unit", "seconds", "--unit-count", "1"))


# ---------------------------------------------------------------- CLI-only sequences
@pytest.mark.timeout(600)
def test_init_after_rollover_and_lookback_restores_nothing(cluster, prefix):
    p = prefix + "-"
    init(cluster, prefix)
    roll(cluster, prefix)
    init(cluster, prefix)
    assert cluster.indices(p) == family_layout(p, [1, 2])
    time.sleep(2.2)
    lookback_seconds(cluster, prefix)
    init(cluster, prefix)
    assert cluster.indices(p) == family_layout(p, [1, 2], read=[2])


def test_init_reattaches_missing_read_alias_to_first_generation(cluster, prefix):
    p = prefix + "-"
    init(cluster, prefix)
    roll(cluster, prefix)
    roll(cluster, prefix)
    status, _ = cluster.request("POST", "/_aliases", {"actions": [{"remove": {"index": f"{p}jaeger-span-*", "alias": f"{p}jaeger-span-read"}}]})
    assert status == 200
    init(cluster, prefix)
    # The read alias is recreated on the initial index, not on the current write index.
    assert cluster.indices(p) == family_layout(p, [1, 2, 3], read={"span": [1], "service": [1, 2, 3], "dependencies": [1, 2, 3]})


def test_rollover_attaches_read_alias_only_to_new_generation(cluster, prefix):
    p = prefix + "-"
    init(cluster, prefix)
    status, _ = cluster.request("POST", "/_aliases", {"actions": [{"remove": {"index": f"{p}jaeger-service-*", "alias": f"{p}jaeger-service-read"}}]})
    assert status == 200
    roll(cluster, prefix)
    assert cluster.indices(p) == family_layout(p, [1, 2], read={"span": [1, 2], "service": [2], "dependencies": [1, 2]})


def test_lookback_after_more_rollovers_only_detaches_older(cluster, prefix):
    p = prefix + "-"
    init(cluster, prefix)
    roll(cluster, prefix)
    time.sleep(2.2)
    lookback_seconds(cluster, prefix)
    roll(cluster, prefix)
    # Generation 2 lost the write alias seconds ago but was created recently; a 60-minute lookback keeps it.
    ok(rollover_tool("lookback", cluster.url, "--index-prefix", prefix, "--unit", "minutes", "--unit-count", "60"))
    assert cluster.indices(p) == family_layout(p, [1, 2, 3], read=[2, 3])


# ---------------------------------------------------------------- with the trace server
def two_spans(trace_id, first_id, service, ops, start):
    return payload(resource_spans(service, [otlp_span(trace_id, f"{first_id + i:016x}", op, start + i * 1000, 500)
                                            for i, op in enumerate(ops)]))


def service_docs(cluster, prefix):
    cluster.refresh(prefix)
    return sorted((h["_index"], h["_source"]["serviceName"], h["_source"]["operationName"])
                  for h in cluster.search(f"{prefix}jaeger-service-read,{prefix}jaeger-service-*"))


def test_server_start_installs_span_and_service_templates(cluster, prefix):
    p = prefix + "-"
    ok(rollover_tool("init", cluster.url, "--index-prefix", prefix, "--shards", "2", "--replicas", "0"))
    with trace_server(cluster.url, prefix, shards=1, replicas=0):
        templates = cluster.templates(p)
        assert_template(templates[f"{p}jaeger-span"], expected_template("jaeger-span", p, shards=1))
        assert_template(templates[f"{p}jaeger-service"], expected_template("jaeger-service", p, shards=1))
        assert_template(templates[f"{p}jaeger-dependencies"], expected_template("jaeger-dependencies", p, shards=2))
        assert cluster.indices(p) == family_layout(p, [1])
        assert cluster.settings(f"{p}jaeger-span-000001")["index.number_of_shards"] == "2"
        roll(cluster, prefix)
        for family, shards in (("span", "1"), ("service", "1"), ("dependencies", "2")):
            assert cluster.settings(f"{p}jaeger-{family}-000002")["index.number_of_shards"] == shards


def test_server_without_init_writes_to_alias_names_as_indices(cluster, prefix):
    p = prefix + "-"
    trace = "11" * 16
    with trace_server(cluster.url, prefix) as server:
        assert sorted(cluster.templates(p)) == [f"{p}jaeger-service", f"{p}jaeger-span"]
        assert cluster.indices(p) == {}
        server.ingest(two_spans(trace, 1, "lost", ["op"], BASE))
        wait_docs(cluster, p, f"{p}jaeger-span-write", 1)
        assert cluster.indices(p) == {f"{p}jaeger-span-write": [], f"{p}jaeger-service-write": []}
        assert server.get("/api/v3/services") == (200, {"services": []})
        assert server.get(f"/api/v3/traces/{trace}")[0] == 404


def test_server_with_trailing_dash_prefix(cluster, prefix):
    p = prefix + "-"
    trace = "22" * 16
    ok(rollover_tool("init", cluster.url, "--index-prefix", p, "--shards", "1", "--replicas", "0"))
    with trace_server(cluster.url, p) as server:
        server.ingest(two_spans(trace, 1, "zed", ["zop"], BASE))
        hits = wait_docs(cluster, p, f"{p}jaeger-span-read", 1)
        assert [h["_index"] for h in hits] == [f"{p}jaeger-span-000001"]
        wait_docs(cluster, p, f"{p}jaeger-service-read", 1)
        assert cluster.indices(p) == family_layout(p, [1])
        assert cluster.indices(p + "-") == {}
        assert server.get("/api/v3/services") == (200, {"services": ["zed"]})
        status, body = server.get(f"/api/v3/traces/{trace}")
        assert status == 200 and span_ids(body["result"]) == ["0000000000000001"]


def test_rollover_and_lookback_under_a_running_server(cluster, prefix):
    p = prefix + "-"
    old, new, shared = "a1" * 16, "f6" * 16, "c3" * 16
    init(cluster, prefix)
    with trace_server(cluster.url, prefix) as server:
        server.ingest(payload(
            resource_spans("alpha", [otlp_span(old, "0000000000000001", "GET /cart", BASE, 5000, kind=2),
                                     otlp_span(old, "0000000000000002", "SELECT", BASE + 1000, 2000, parent="0000000000000001", kind=3),
                                     otlp_span(shared, "00000000000000c1", "early", BASE + 100_000, 1000)]),
            resource_spans("beta", [otlp_span(old, "0000000000000003", "auth", BASE + 1500, 500, parent="0000000000000001")])))
        wait_docs(cluster, p, f"{p}jaeger-span-read", 4)
        wait_docs(cluster, p, f"{p}jaeger-service-read", 4)
        cluster.refresh(p)
        roll(cluster, prefix, '{"max_docs": 1}')
        # Only families holding documents roll over.
        assert cluster.indices(p) == family_layout(p, {"span": [1, 2], "service": [1, 2], "dependencies": [1]})

        server.ingest(payload(
            resource_spans("alpha", [otlp_span(old, "0000000000000004", "late", BASE + 4000, 500, parent="0000000000000001"),
                                     otlp_span(old, "0000000000000005", "SELECT", BASE + 4500, 300, parent="0000000000000001", kind=3),
                                     otlp_span(shared, "00000000000000c2", "later", BASE + 200_000, 1000)]),
            resource_spans("gamma", [otlp_span(new, "00000000000000f1", "new", BASE + 30_000_000_000, 1000, kind=2)])))
        hits = wait_docs(cluster, p, f"{p}jaeger-span-read", 8)
        placement = sorted((h["_index"], h["_source"]["spanID"]) for h in hits)
        assert placement == sorted([(f"{p}jaeger-span-000001", s) for s in ("0000000000000001", "0000000000000002", "0000000000000003", "00000000000000c1")]
                                   + [(f"{p}jaeger-span-000002", s) for s in ("0000000000000004", "0000000000000005", "00000000000000c2", "00000000000000f1")])
        time.sleep(1.5)
        # A pair this server process already wrote is not written again into the new generation.
        assert service_docs(cluster, p) == sorted(
            [(f"{p}jaeger-service-000001", "alpha", op) for op in ("GET /cart", "SELECT", "early")]
            + [(f"{p}jaeger-service-000001", "beta", "auth")]
            + [(f"{p}jaeger-service-000002", "alpha", "late"), (f"{p}jaeger-service-000002", "alpha", "later"),
               (f"{p}jaeger-service-000002", "gamma", "new")])

        # Reads span both generations through the read alias.
        status, body = server.get(f"/api/v3/traces/{old}")
        assert status == 200
        assert span_ids(body["result"]) == [f"{i:016x}" for i in range(1, 6)]
        status, body = server.get("/api/v3/traces", {**DAY, "query.service_name": "alpha"})
        assert status == 200 and trace_ids(body["result"]) == sorted([old, shared])
        status, body = server.get("/api/v3/services")
        assert status == 200 and sorted(body["services"]) == ["alpha", "beta", "gamma"]

        time.sleep(2.2)
        lookback_seconds(cluster, prefix)
        assert cluster.indices(p) == family_layout(p, {"span": [1, 2], "service": [1, 2], "dependencies": [1]},
                                                   read={"span": [2], "service": [2], "dependencies": [1]})
        status, body = server.get(f"/api/v3/traces/{old}")
        assert status == 200 and span_ids(body["result"]) == ["0000000000000004", "0000000000000005"]
        status, body = server.get(f"/api/traces/{old}")
        assert status == 200 and sorted(s["spanID"] for s in body["data"][0]["spans"]) == ["0000000000000004", "0000000000000005"]
        status, body = server.get("/api/v3/traces", {**DAY, "query.service_name": "alpha", "query.operation_name": "GET /cart"})
        assert status == 404
        status, body = server.get("/api/v3/traces", {**DAY, "query.service_name": "alpha", "query.operation_name": "SELECT"})
        assert status == 200 and span_ids(body["result"]) == ["0000000000000004", "0000000000000005"]
        status, body = server.get("/api/v3/services")
        assert status == 200 and sorted(body["services"]) == ["alpha", "gamma"]
        status, body = server.get("/api/v3/operations", {"service": "alpha"})
        assert status == 200 and sorted(o["name"] for o in body["operations"]) == ["late", "later"]
        status, body = server.get("/api/v3/operations", {"service": "beta"})
        assert status == 200 and body == {"operations": []}
        # Detached indices still hold their documents.
        assert len(cluster.search(f"{p}jaeger-span-000001")) == 4


def test_restarted_server_writes_known_pairs_into_current_generation(cluster, prefix):
    p = prefix + "-"
    trace = "e7" * 16
    init(cluster, prefix)
    with trace_server(cluster.url, prefix) as server:
        server.ingest(two_spans(trace, 1, "svc", ["first", "second"], BASE))
        wait_docs(cluster, p, f"{p}jaeger-service-read", 2)
        cluster.refresh(p)
        roll(cluster, prefix, '{"max_docs": 1}')
        server.ingest(two_spans(trace, 3, "svc", ["first", "third"], BASE + 3000))
        wait_docs(cluster, p, f"{p}jaeger-span-read", 4)
        time.sleep(1.5)
        assert service_docs(cluster, p) == sorted([(f"{p}jaeger-service-000001", "svc", "first"), (f"{p}jaeger-service-000001", "svc", "second"),
                                                   (f"{p}jaeger-service-000002", "svc", "third")])
    with trace_server(cluster.url, prefix) as server:
        server.ingest(two_spans(trace, 5, "svc", ["first", "second"], BASE + 5000))
        wait_docs(cluster, p, f"{p}jaeger-span-read", 6)
        wait_until(lambda: len(service_docs(cluster, p)) >= 5, timeout=10)
        assert service_docs(cluster, p) == sorted([(f"{p}jaeger-service-000001", "svc", "first"), (f"{p}jaeger-service-000001", "svc", "second"),
                                                   (f"{p}jaeger-service-000002", "svc", "third"),
                                                   (f"{p}jaeger-service-000002", "svc", "first"), (f"{p}jaeger-service-000002", "svc", "second")])
