"""Multi-step scenarios: mixed requests, reservation/refund cycles, reloads with live counters."""

import time

from support import (
    OK_BARE,
    UNLIMITED,
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


def test_mixed_request_overall_code(valkey, launch, domain):
    server = launch({"config/limits.yaml": config(domain, """
        - key: user
          rate_limit: {unit: hour, requests_per_unit: 2}
        - key: ip
          rate_limit: {unit: minute, requests_per_unit: 5}
        - key: free
          rate_limit: {unlimited: true}
        - key: shadow
          shadow_mode: true
          rate_limit: {unit: hour, requests_per_unit: 1}
    """)})
    settle("MINUTE", "HOUR", margin=15)
    h, m = window_start("HOUR"), window_start("MINUTE")
    U, IP, SH = (2, "HOUR"), (5, "MINUTE"), (1, "HOUR")
    ua, ub, ip, sh = f"{domain}_user_a_{h}", f"{domain}_user_b_{h}", f"{domain}_ip_1_{m}", f"{domain}_shadow_s_{h}"
    steps = [
        ([d(("user", "a")), d(("ip", "1")), d(("free", "x")), d(("nomatch", "z"))], "OK",
         [ok(U, 1), ok(IP, 4), UNLIMITED, OK_BARE], {ua: 1, ip: 1}),
        # one rejected descriptor rejects the request, yet every descriptor is counted
        ([d(("user", "a"), hitsAddend=2), d(("ip", "1")), d(("shadow", "s"), hitsAddend=4)], "OVER_LIMIT",
         [over(U), ok(IP, 3), ok(SH, 0)], {ua: 3, ip: 2, sh: 4}),
        ([d(("ip", "1")), d(("user", "b"))], "OK", [ok(IP, 2), ok(U, 1)], {ua: 3, ip: 3, sh: 4, ub: 1}),
    ]
    for descriptors, overall, statuses, state in steps:
        reply, span = server.post({"domain": domain, "descriptors": descriptors})
        check_response(reply, span, overall, statuses)
        assert counters(valkey["main"], domain) == state, descriptors


def test_reservation_and_refund_cycle(valkey, launch, domain):
    server = launch({"config/limits.yaml": config(domain, """
        - key: jobs
          rate_limit: {unit: hour, requests_per_unit: 3, name: jobs}
    """)})
    settle("HOUR")
    key = f"{domain}_jobs_tenant_{window_start('HOUR')}"
    J = (3, "HOUR")
    steps = [
        ({"hitsAddend": 2}, "OK", ok(J, 1, "jobs"), 2),
        ({"hitsAddend": 2}, "OVER_LIMIT", over(J, "jobs"), 4),
        # roll back the rejected reservation
        ({"hitsAddend": 2, "negative": True}, "OK", ok(J, 1, "jobs"), 2),
        ({}, "OK", ok(J, 0, "jobs"), 3),
        ({}, "OVER_LIMIT", over(J, "jobs"), 4),
        ({"hitsAddend": 4, "negative": True}, "OK", ok(J, 3, "jobs"), 0),
        ({"hitsAddend": 3}, "OK", ok(J, 0, "jobs"), 3),
    ]
    for options, overall, status, count in steps:
        descriptor = d(("jobs", "tenant"))
        if "hitsAddend" in options:
            descriptor["hitsAddend"] = options["hitsAddend"]
        if options.get("negative"):
            descriptor["isNegativeHits"] = True
        reply, span = server.post({"domain": domain, "descriptors": [descriptor]})
        check_response(reply, span, overall, [status])
        assert counters(valkey["main"], domain) == {key: count}, options


def test_reload_keeps_counters(valkey, launch, runtime, domain):
    rule = lambda n: config(domain, f"- key: k\n  rate_limit: {{unit: hour, requests_per_unit: {n}}}")  # noqa: E731
    server = launch({"config/limits.yaml": rule(5)})
    settle("HOUR", margin=40)
    key = f"{domain}_k_v_{window_start('HOUR')}"
    reply, span = server.post({"domain": domain, "descriptors": [d(("k", "v"), hitsAddend=4)]})
    check_response(reply, span, "OK", [ok((5, "HOUR"), 1)])

    def until(expected):
        deadline = time.time() + 10
        while True:
            reply, span = server.post({"domain": domain, "descriptors": [d(("k", "v"), hitsAddend=0)]})
            try:
                return check_response(reply, span, expected[0], [expected[1]])
            except AssertionError:
                if time.time() > deadline:
                    raise
                time.sleep(0.3)

    runtime.write("config/limits.yaml", rule(3))
    until(("OVER_LIMIT", over((3, "HOUR"))))
    reply, span = server.post({"domain": domain, "descriptors": [d(("k", "v"))]})
    check_response(reply, span, "OVER_LIMIT", [over((3, "HOUR"))])
    assert counters(valkey["main"], domain) == {key: 5}

    runtime.write("config/limits.yaml", rule(10))
    until(("OK", ok((10, "HOUR"), 5)))
    reply, span = server.post({"domain": domain, "descriptors": [d(("k", "v"))]})
    check_response(reply, span, "OK", [ok((10, "HOUR"), 4)])
    assert counters(valkey["main"], domain) == {key: 6}


def test_grpc_and_json_with_prefix_and_split(valkey, launch, domain, key_prefixes):
    prefix = f"q{domain}-"
    key_prefixes.append(prefix)
    server = launch({"config/limits.yaml": config(domain, """
        - key: u
          rate_limit: {unit: hour, requests_per_unit: 3, name: urule}
        - key: burst
          rate_limit: {unit: second, requests_per_unit: 4}
    """)}, CACHE_KEY_PREFIX=prefix, REDIS_PERSECOND="true", REDIS_PERSECOND_SOCKET_TYPE="tcp", REDIS_PERSECOND_URL=valkey["persec"])
    stub, pb = server.grpc()
    D = pb.RateLimitDescriptor
    settle("HOUR")
    u = f"{prefix}{domain}_u_g_{window_start('HOUR')}"
    U, B = (3, "HOUR"), (4, "SECOND")

    span = Span()
    response = stub.ShouldRateLimit(pb.RateLimitRequest(domain=domain, hits_addend=2, descriptors=[
        D(entries=[D.Entry(key="u", value="g")]),
        D(entries=[D.Entry(key="burst", value="b")]),
        D(entries=[D.Entry(key="burst", value="b")], is_negative_hits=True),
    ]), timeout=10)
    persec = counters(valkey["persec"], prefix)
    span.close()
    check_grpc(response, pb, span, "OK", [ok(U, 1, "urule"), ok(B, 2), ok(B, 4)])
    assert counters(valkey["main"], prefix) == {u: 2}
    assert len(persec) == 1 and list(persec.values()) == [0], persec
    assert next(iter(persec)).startswith(f"{prefix}{domain}_burst_b_"), persec

    reply, span = server.post({"domain": domain, "descriptors": [d(("u", "g")), d(("burst", "c"), hitsAddend=5)]})
    persec = counters(valkey["persec"], prefix + domain + "_burst_c_")
    check_response(reply, span, "OVER_LIMIT", [ok(U, 0, "urule"), over(B)])
    assert counters(valkey["main"], prefix) == {u: 3}
    assert list(persec.values()) == [5], persec
    assert counters(valkey["main"], domain) == {} and counters(valkey["persec"], domain) == {}
