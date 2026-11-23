"""Query API over the read aliases: services, operations, trace lookup (APIv3 and legacy) and trace search."""

import json
from pathlib import Path

import pytest

from sample_traces import TA, TB, TC, TD, rich_payload
from support import DAY, legacy_trace, ok, otlp_flat, rollover_tool, trace_ids, trace_server, unique_prefix, wait_docs

EXPECTED = json.loads((Path(__file__).parent / "expected" / "rich_trace.json").read_text())


@pytest.fixture(scope="module")
def server(cluster):
    base = unique_prefix("q")
    try:
        ok(rollover_tool("init", cluster.url, "--index-prefix", base, "--shards", "1", "--replicas", "0"))
        with trace_server(cluster.url, base) as srv:
            srv.ingest(rich_payload())
            wait_docs(cluster, base + "-", f"{base}-jaeger-span-read", 10)
            wait_docs(cluster, base + "-", f"{base}-jaeger-service-read", 10)
            yield srv
    finally:
        cluster.cleanup(base)


def error_body(body, code):
    assert isinstance(body, dict) and set(body) == {"error"}, body
    assert set(body["error"]) == {"httpCode", "message"}, body
    assert type(body["error"]["httpCode"]) is int and body["error"]["httpCode"] == code
    assert isinstance(body["error"]["message"], str)
    return body["error"]["message"]


@pytest.mark.timeout(600)
def test_services(server):
    status, body = server.get("/api/v3/services")
    assert status == 200 and set(body) == {"services"}
    assert sorted(body["services"]) == ["alpha", "beta", "missing-service-name"]


@pytest.mark.parametrize("params, names", [
    ({"service": "alpha"}, ["GET /cart", "SELECT", "compute", "consume", "publish", "unspecified"]),
    ({"service": "beta"}, ["GET /cart", "batch"]),
    ({"service": "missing-service-name"}, ["lonely", "orphan"]),
    ({"service": "alpha", "span_kind": "server"}, ["GET /cart", "SELECT", "compute", "consume", "publish", "unspecified"]),
    ({"service": "alpha", "spanKind": "client"}, ["GET /cart", "SELECT", "compute", "consume", "publish", "unspecified"]),
    ({"service": "zzz"}, []),
    ({}, []),
])
def test_operations(server, params, names):
    status, body = server.get("/api/v3/operations", params)
    assert status == 200 and set(body) == {"operations"}
    for op in body["operations"]:
        assert set(op) == {"name", "spanKind"}, op
    assert sorted((op["name"], op["spanKind"]) for op in body["operations"]) == sorted((n, "internal") for n in names)


@pytest.mark.parametrize("trace_id, key", [(TA, "TA"), (TB, "TB"), (TC, "TC"), (TD, "TD")])
def test_get_trace_v3(server, trace_id, key):
    status, body = server.get(f"/api/v3/traces/{trace_id}")
    assert status == 200 and set(body) == {"result"}
    assert otlp_flat(body["result"]) == otlp_flat(EXPECTED[f"v3 {key}"])


def test_get_trace_v3_raw_and_uppercase_id(server):
    expected = otlp_flat(EXPECTED["v3 TA"])
    status, body = server.get(f"/api/v3/traces/{TA}", {"raw_traces": "true"})
    assert status == 200 and otlp_flat(body["result"]) == expected
    status, body = server.get(f"/api/v3/traces/{TA.upper()}")
    assert status == 200 and otlp_flat(body["result"]) == expected


@pytest.mark.parametrize("trace_id, key", [(TA, "TA"), (TB, "TB"), (TC, "TC")])
def test_get_trace_legacy(server, trace_id, key):
    status, body = server.get(f"/api/traces/{trace_id}")
    assert status == 200
    assert set(body) == {"data", "total", "limit", "offset", "errors"}
    assert legacy_trace(body) == legacy_trace(EXPECTED[f"legacy {key}"])


def test_get_trace_not_found_and_malformed(server):
    missing = "e5" * 16
    assert server.get(f"/api/v3/traces/{missing}") == (404, {"error": {"httpCode": 404, "message": "No traces found"}})
    status, body = server.get(f"/api/v3/traces/{TA[16:]}")  # 64-bit form of a different (zero-padded) id
    assert status == 404
    status, body = server.get("/api/v3/traces/xyz")
    assert status == 400 and error_body(body, 400)
    status, body = server.get(f"/api/v3/traces/{TA}", {"raw_traces": "maybe"})
    assert status == 400 and error_body(body, 400)
    assert server.get(f"/api/traces/{missing}") == (404, {"data": None, "total": 0, "limit": 0, "offset": 0,
                                                          "errors": [{"code": 404, "msg": "trace not found"}]})
    status, body = server.get("/api/traces/xyz")
    assert status == 400 and body["data"] is None and [e["code"] for e in body["errors"]] == [400]


def search(server, params):
    status, body = server.get("/api/v3/traces", params)
    if status == 404:
        error_body(body, 404)
        assert body["error"]["message"] == "No traces found"
        return status, []
    assert status == 200, (status, body)
    return status, trace_ids(body["result"])


def attrs(service, **pairs):
    return {**DAY, **({"query.service_name": service} if service else {}), "query.attributes": json.dumps(pairs)}


@pytest.mark.parametrize("params, expected", [
    (DAY, [TA, TB, TC, TD]),
    ({**DAY, "query.service_name": "alpha"}, [TA]),
    ({**DAY, "query.service_name": "beta"}, [TA, TB]),
    ({**DAY, "query.service_name": "OTLPResourceNoServiceName"}, []),
    ({**DAY, "query.service_name": "alpha", "query.operation_name": "SELECT"}, [TA]),
    ({**DAY, "query.service_name": "beta", "query.operation_name": "SELECT"}, []),
    ({**DAY, "query.operation_name": "GET /cart"}, [TA]),
    ({"query.serviceName": "beta", "query.startTimeMin": DAY["query.start_time_min"], "query.startTimeMax": DAY["query.start_time_max"]}, [TA, TB]),
])
def test_search_by_service_and_operation(server, params, expected):
    assert search(server, params) == ((200 if expected else 404), sorted(expected))


@pytest.mark.parametrize("params, expected", [
    (attrs("beta", tier="gold"), [TA]),
    (attrs("beta", tier="g.*"), [TA]),
    (attrs("beta", tier="gol"), []),
    (attrs("beta", pattern="abc*"), []),
    (attrs("beta", pattern="abc\\*"), [TB]),
    (attrs("alpha", retries="3"), [TA]),
    (attrs("alpha", ok="true"), [TA]),
    (attrs("alpha", ratio="1.5"), [TA]),
    (attrs("alpha", blob="0102ff"), [TA]),
    (attrs("alpha", **{"host.name": "h1"}), [TA]),
    (attrs("alpha", key="c1"), [TA]),
    (attrs("alpha", event="cache miss"), [TA]),
    (attrs("alpha", **{"span.kind": "server"}), [TA]),
    (attrs("alpha", **{"otel.status_code": "OK"}), [TA]),
    (attrs("alpha", **{"otel.scope.name": "lib.http"}), [TA]),
    (attrs("alpha", error="true"), [TA]),
    (attrs("alpha", error="false"), [TA]),
    (attrs("beta", error="false"), [TA, TB]),
    (attrs("alpha", **{"http.method": "GET", "db.system": "pg"}), []),
])
def test_search_by_attributes(server, params, expected):
    assert search(server, params) == ((200 if expected else 404), sorted(expected))


@pytest.mark.parametrize("params, expected", [
    ({**DAY, "query.service_name": "alpha", "query.duration_min": "5ms"}, [TA]),
    ({**DAY, "query.service_name": "alpha", "query.duration_min": "5001us"}, []),
    ({**DAY, "query.service_name": "alpha", "query.duration_max": "100us"}, [TA]),
    ({**DAY, "query.service_name": "alpha", "query.duration_max": "99us"}, []),
    ({**DAY, "query.duration_min": "1s"}, [TB]),
    ({**DAY, "query.search_depth": "1"}, [TD]),
    ({**DAY, "query.search_depth": "2"}, [TC, TD]),
    ({**DAY, "query.search_depth": "0"}, [TA, TB, TC, TD]),
    ({"query.start_time_min": "2017-01-26T16:46:31Z", "query.start_time_max": "2017-01-26T16:46:31.001Z"}, [TA]),
    ({"query.start_time_min": "2017-01-26T16:46:41Z", "query.start_time_max": "2017-01-26T16:46:51Z"}, [TC, TD]),
    ({"query.start_time_min": "2018-01-27T00:00:00Z", "query.start_time_max": "2018-01-28T00:00:00Z"}, []),
])
def test_search_by_duration_depth_and_window(server, params, expected):
    assert search(server, params) == ((200 if expected else 404), sorted(expected))


def test_search_returns_complete_matching_traces(server):
    status, body = server.get("/api/v3/traces", {**DAY, "query.service_name": "beta"})
    assert status == 200 and set(body) == {"result"}
    assert otlp_flat(body["result"]) == sorted(otlp_flat(EXPECTED["v3 TA"]) + otlp_flat(EXPECTED["v3 TB"]),
                                               key=lambda s: (s["traceId"], s["spanId"]))


@pytest.mark.parametrize("params", [
    {},
    {"query.service_name": "alpha"},
    {"query.start_time_min": "2017-01-27T00:00:00Z", "query.start_time_max": "2017-01-26T00:00:00Z"},
    {"query.start_time_min": "2017-01-26T16:46:31Z", "query.start_time_max": "2017-01-26T16:46:31Z"},
    {"query.start_time_min": "yesterday", "query.start_time_max": "2017-01-26T00:00:00Z"},
    {**DAY, "query.duration_min": "10ms", "query.duration_max": "1ms"},
    {**DAY, "query.duration_min": "bogus"},
    {**DAY, "query.search_depth": "-1"},
    {**DAY, "query.attributes": '{"x": 3}'},
    {**DAY, "query.attributes": "{bad"},
])
def test_search_rejects_invalid_queries(server, params):
    status, body = server.get("/api/v3/traces", params)
    assert status == 400
    error_body(body, 400)
