"""Service surface: process lifecycle, /json encoding and errors, and the gRPC interface."""

import json
import signal

import grpc
import pytest

from support import (
    OK_BARE,
    Span,
    check_grpc,
    check_response,
    config,
    counters,
    d,
    ok,
    over,
    settle,
    window_start,
)

H3 = (3, "HOUR")
RULES = """
    - key: u
      rate_limit: {unit: hour, requests_per_unit: 3, name: urule}
    - key: plain
      rate_limit: {unit: hour, requests_per_unit: 3}
"""


def test_healthcheck_and_sigterm(launch, domain):
    server = launch({"config/limits.yaml": config(domain, RULES)})
    health = server.get("/healthcheck")
    assert health.status == 200 and health.text.strip() == "OK", (health.status, health.text)
    server.process.send_signal(signal.SIGTERM)
    server.process.wait(timeout=10)


def test_json_reply_encoding(valkey, launch, domain):
    server = launch({"config/limits.yaml": config(domain, RULES)})
    settle("HOUR")
    for overall, status in (("OK", ok(H3, 0)), ("OVER_LIMIT", over(H3))):
        body = {"domain": domain, "descriptors": [d(("plain", "p"), hitsAddend=3 if overall == "OK" else 1)]}
        reply, span = server.post(body)
        assert reply.content_type.split(";")[0].strip() == "application/json", reply.content_type
        check_response(reply, span, overall, [status])


def test_request_field_spellings(valkey, launch, domain):
    server = launch({"config/limits.yaml": config(domain, RULES)})
    settle("HOUR")
    w = window_start("HOUR")
    k = lambda v: f"{domain}_plain_{v}_{w}"  # noqa: E731
    bodies = [
        ({"domain": domain, "hits_addend": 2, "descriptors": [{"entries": [{"key": "plain", "value": "a"}]}]}, [ok(H3, 1)], {k("a"): 2}),
        ({"domain": domain, "descriptors": [{"entries": [{"key": "plain", "value": "a"}], "is_negative_hits": True, "hits_addend": "2"}]},
         [ok(H3, 3)], {k("a"): 0}),
        ({"domain": domain, "hitsAddend": "2", "descriptors": [d(("plain", "b"), hitsAddend=1.0), d(("plain", "c"))]},
         [ok(H3, 2), ok(H3, 1)], {k("a"): 0, k("b"): 1, k("c"): 2}),
    ]
    for body, statuses, state in bodies:
        reply, span = server.post(body)
        check_response(reply, span, "OK", statuses)
        assert counters(valkey["main"], domain) == state, body


def test_rejected_requests(valkey, launch, domain):
    server = launch({"config/limits.yaml": config(domain, RULES)})
    entry = [d(("plain", "e"))]
    bad = [
        "not json",
        "",
        {"domain": "", "descriptors": entry},
        {"domain": domain},
        {"domain": domain, "descriptors": []},
        {"domain": domain, "bogus": 1, "descriptors": entry},
        {"domain": domain, "hitsAddend": -1, "descriptors": entry},
        {"domain": domain, "descriptors": [d(("plain", "e"), isNegativeHits="true")]},
    ]
    for body in bad:
        reply = server.post_raw(body if isinstance(body, str) else json.dumps(body))
        assert reply.status == 400, f"{body!r}: HTTP {reply.status} {reply.text}"
    reply = server.get("/json")
    assert reply.status == 400, reply.text
    assert counters(valkey["main"], domain) == {}
    # The service keeps serving afterwards.
    settle("HOUR")
    reply, span = server.post({"domain": domain, "descriptors": entry})
    check_response(reply, span, "OK", [ok(H3, 2)])


def test_unknown_domain_and_unmatched_descriptors(valkey, launch, domain):
    server = launch({"config/limits.yaml": config(domain, RULES)})
    unknown = domain + "x"
    reply, span = server.post({"domain": unknown, "descriptors": [d(("plain", "a")), d(("u", "b"))]})
    check_response(reply, span, "OK", [OK_BARE, OK_BARE])
    reply, span = server.post({"domain": domain, "descriptors": [d(("nomatch", "a")), {"entries": []}, d(("plain", "a"), ("extra", "b"))]})
    check_response(reply, span, "OK", [OK_BARE, OK_BARE, OK_BARE])
    assert counters(valkey["main"], domain) == {}


def _request(pb, domain, *descriptors, hits=0):
    return pb.RateLimitRequest(domain=domain, hits_addend=hits, descriptors=list(descriptors))


def _desc(pb, *pairs, hits=None, negative=False):
    from google.protobuf import wrappers_pb2

    D = pb.RateLimitDescriptor
    descriptor = D(entries=[D.Entry(key=k, value=v) for k, v in pairs], is_negative_hits=negative)
    if hits is not None:
        descriptor.hits_addend.CopyFrom(wrappers_pb2.UInt64Value(value=hits))
    return descriptor


def _call(stub, request):
    span = Span()
    response = stub.ShouldRateLimit(request, timeout=10)
    return response, span.close()


def test_grpc_shares_counters_with_json(valkey, launch, domain):
    server = launch({"config/limits.yaml": config(domain, RULES)})
    stub, pb = server.grpc()
    settle("HOUR")
    w = window_start("HOUR")
    g, g2 = f"{domain}_u_g_{w}", f"{domain}_u_g2_{w}"

    response, span = _call(stub, _request(pb, domain, _desc(pb, ("u", "g"))))
    check_grpc(response, pb, span, "OK", [ok(H3, 2, "urule")])
    assert counters(valkey["main"], domain) == {g: 1}

    reply, span = server.post({"domain": domain, "descriptors": [d(("u", "g"))]})
    check_response(reply, span, "OK", [ok(H3, 1, "urule")])
    assert counters(valkey["main"], domain) == {g: 2}

    response, span = _call(stub, _request(pb, domain, _desc(pb, ("u", "g")), _desc(pb, ("u", "g2"), hits=0), _desc(pb, ("nomatch", "x")), hits=5))
    check_grpc(response, pb, span, "OVER_LIMIT", [over(H3, "urule"), ok(H3, 3, "urule"), OK_BARE])
    assert counters(valkey["main"], domain) == {g: 7, g2: 0}

    response, span = _call(stub, _request(pb, domain, _desc(pb, ("u", "g"), hits=5, negative=True)))
    check_grpc(response, pb, span, "OK", [ok(H3, 1, "urule")])
    assert counters(valkey["main"], domain) == {g: 2, g2: 0}

    reply, span = server.post({"domain": domain, "descriptors": [d(("u", "g"))]})
    check_response(reply, span, "OK", [ok(H3, 0, "urule")])
    assert counters(valkey["main"], domain) == {g: 3, g2: 0}


def test_grpc_errors_and_health(valkey, launch, domain):
    server = launch({"config/limits.yaml": config(domain, RULES)})
    stub, pb = server.grpc()
    for request in (_request(pb, "", _desc(pb, ("u", "g"))), _request(pb, domain)):
        with pytest.raises(grpc.RpcError) as error:
            stub.ShouldRateLimit(request, timeout=10)
        assert error.value.code() == grpc.StatusCode.UNKNOWN, error.value
    response, span = _call(stub, _request(pb, domain + "x", _desc(pb, ("u", "g"))))
    check_grpc(response, pb, span, "OK", [OK_BARE])
    assert counters(valkey["main"], domain) == {}
    # grpc.health.v1.Health/Check for the server as a whole: HealthCheckResponse{status: SERVING}.
    check = server.grpc_channel().unary_unary("/grpc.health.v1.Health/Check", request_serializer=bytes, response_deserializer=bytes)
    assert check(b"", timeout=10) == b"\x08\x01"
