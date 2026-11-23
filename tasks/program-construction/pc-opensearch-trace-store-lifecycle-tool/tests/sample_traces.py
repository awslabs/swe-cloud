"""OTLP JSON fixtures (public OTLP encoding) with distinct values per mechanism under test."""

from support import BASE, attr, otlp_span, payload, resource_spans

TA = "a1" * 16  # multi-service trace: every attribute type, events, links, statuses, all span kinds
TB = "b2" * 16  # older single-span trace, linked from TA
TC = "c3" * 16  # resource without attributes
TD = "d4" * 16  # resource without service.name, zero-length span
ROOT = "0000000000000001"


def rich_payload():
    alpha = {"host.name": "h1", "pid": 42, "load": 0.5, "canary": True}
    http_scope = {"name": "lib.http", "version": "1.2.3", "attributes": [attr("scope.attr", "s")]}
    root = otlp_span(
        TA, ROOT, "GET /cart", BASE, 5_000_000, kind=2,
        attrs={"http.method": "GET", "retries": 3, "ratio": 1.5, "whole": 2.0, "ok": True,
               "blob": b"\x01\x02\xff", "list": ["a", 1], "map": {"k": "v"}},
        traceState="vendor=abc", flags=1,
        events=[{"timeUnixNano": str(BASE + 1_000_000), "name": "cache miss", "attributes": [attr("key", "c1"), attr("size", 7)]},
                {"timeUnixNano": str(BASE + 2_000_000), "name": "named", "attributes": [attr("event", "override")]},
                {"timeUnixNano": str(BASE + 3_000_000), "name": ""}],
        status={"code": 1})
    select = otlp_span(
        TA, "0000000000000002", "SELECT", BASE + 1_000_000, 2_500_000, parent=ROOT, kind=3,
        attrs={"db.system": "pg"},
        links=[{"traceId": TB, "spanId": "00000000000000b1", "attributes": [attr("why", "batch")]},
               {"traceId": TA, "spanId": ROOT, "attributes": [attr("opentracing.ref_type", "child_of")]}],
        status={"code": 2, "message": "boom"})
    unscoped = [
        otlp_span(TA, "0000000000000003", "publish", BASE + 3_600_000, 400_000, parent=ROOT, kind=4, status={"code": 2}),
        otlp_span(TA, "0000000000000004", "consume", BASE + 3_700_000, 200_000, parent="0000000000000003", kind=5),
        otlp_span(TA, "0000000000000005", "compute", BASE + 4_000_000, 100_000, parent=ROOT, kind=1,
                  status={"code": 0, "message": "ignored"}),
        otlp_span(TA, "0000000000000006", "unspecified", BASE + 4_100_000, 100_000, parent=ROOT, kind=0),
    ]
    beta = [
        otlp_span(TA, "0000000000000007", "GET /cart", BASE + 1_500_000, 1_500_000, parent="0000000000000002", kind=2,
                  attrs={"tier": "gold"}),
        otlp_span(TB, "00000000000000b1", "batch", BASE - 60_000_000_000, 1_000_000_000, kind=1,
                  attrs={"tier": "silver", "pattern": "abc*"}),
    ]
    alpha_group = resource_spans("alpha", [root, select], alpha, http_scope)
    alpha_group["scopeSpans"].append({"scope": {}, "spans": unscoped})
    return payload(
        alpha_group,
        resource_spans("beta", beta, scope={"name": "lib.rpc"}),
        {"resource": {}, "scopeSpans": [{"spans": [
            otlp_span(TC, "00000000000000c1", "orphan", BASE + 10_000_000_000, 20_000_000)]}]},
        resource_spans(None, [otlp_span(TD, "00000000000000d1", "lonely", BASE + 20_000_000_000, 0)], {"host.name": "noservice"}),
    )


def rich_span_count():
    return 10
