"""Counter storage: key layout, window alignment, expiry, hit accounting and storage options."""

import pytest

from support import (
    config,
    counters,
    check_response,
    client,
    d,
    ok,
    over,
    settle,
    ttl,
    window_start,
)


def test_fixed_window_counting(valkey, launch, domain):
    server = launch({"config/limits.yaml": config(domain, """
        - key: user
          rate_limit: {unit: hour, requests_per_unit: 3}
    """)})
    settle("HOUR")
    key = f"{domain}_user_alice_{window_start('HOUR')}"
    L = (3, "HOUR")
    steps = [("OK", ok(L, 2)), ("OK", ok(L, 1)), ("OK", ok(L, 0)), ("OVER_LIMIT", over(L)), ("OVER_LIMIT", over(L))]
    for count, (overall, status) in enumerate(steps, start=1):
        reply, span = server.post({"domain": domain, "descriptors": [d(("user", "alice"))]})
        check_response(reply, span, overall, [status])
        # Rejected hits are still added to the counter.
        assert counters(valkey["main"], domain) == {key: count}
        assert 3597 <= ttl(valkey["main"], key) <= 3600
    assert counters(valkey["persec"], domain) == {}


def test_window_alignment_per_unit(valkey, launch, domain):
    server = launch({"config/limits.yaml": config(domain, """
        - key: s
          rate_limit: {unit: second, requests_per_unit: 100}
        - key: m
          rate_limit: {unit: minute, requests_per_unit: 100}
        - key: h
          rate_limit: {unit: hour, requests_per_unit: 100}
        - key: dy
          rate_limit: {unit: day, requests_per_unit: 100}
    """)})
    settle("MINUTE", "HOUR", "DAY", margin=15)
    reply, span = server.post({"domain": domain, "descriptors": [d(("s", "1")), d(("m", "2")), d(("h", "3")), d(("dy", "4"))]})
    main = counters(valkey["main"], domain)
    check_response(reply, span, "OK", [ok((100, "SECOND"), 99), ok((100, "MINUTE"), 99), ok((100, "HOUR"), 99), ok((100, "DAY"), 99)])
    seconds = [k for k in main if k.startswith(f"{domain}_s_1_")]
    assert len(seconds) == 1, main
    assert int(span.start) <= int(seconds[0].rsplit("_", 1)[1]) <= int(span.end)
    expected = {
        seconds[0]: 1,
        f"{domain}_m_2_{window_start('MINUTE')}": 1,
        f"{domain}_h_3_{window_start('HOUR')}": 1,
        f"{domain}_dy_4_{window_start('DAY')}": 1,
    }
    assert main == expected
    # Expiry is the full unit length, not the time left in the window.
    assert 57 <= ttl(valkey["main"], f"{domain}_m_2_{window_start('MINUTE')}") <= 60
    assert 3597 <= ttl(valkey["main"], f"{domain}_h_3_{window_start('HOUR')}") <= 3600
    assert 86397 <= ttl(valkey["main"], f"{domain}_dy_4_{window_start('DAY')}") <= 86400
    # Without the split, second-unit counters live on the main instance.
    assert counters(valkey["persec"], domain) == {}


def test_expiry_refreshed_by_every_counted_operation(valkey, launch, domain):
    server = launch({"config/limits.yaml": config(domain, """
        - key: leaf
          rate_limit: {unit: hour, requests_per_unit: 5}
    """)})
    settle("HOUR")
    key = f"{domain}_leaf_l_{window_start('HOUR')}"
    L = (5, "HOUR")
    main = client(valkey["main"])

    reply, span = server.post({"domain": domain, "descriptors": [d(("leaf", "l"))]})
    check_response(reply, span, "OK", [ok(L, 4)])
    main.expire(key, 100)
    reply, span = server.post({"domain": domain, "descriptors": [d(("leaf", "l"), hitsAddend=0)]})
    check_response(reply, span, "OK", [ok(L, 4)])
    assert counters(valkey["main"], domain) == {key: 1}
    assert ttl(valkey["main"], key) >= 3597

    main.expire(key, 100)
    reply, span = server.post({"domain": domain, "descriptors": [d(("leaf", "l"), isNegativeHits=True)]})
    check_response(reply, span, "OK", [ok(L, 5)])
    assert counters(valkey["main"], domain) == {key: 0}
    assert ttl(valkey["main"], key) >= 3597

    main.expire(key, 100)
    reply, span = server.post({"domain": domain, "descriptors": [d(("leaf", "l"), hitsAddend=7)]})
    check_response(reply, span, "OVER_LIMIT", [over(L)])
    assert counters(valkey["main"], domain) == {key: 7}
    assert ttl(valkey["main"], key) >= 3597


def test_hits_addend_rules(valkey, launch, domain):
    server = launch({"config/limits.yaml": config(domain, """
        - key: leaf
          rate_limit: {unit: hour, requests_per_unit: 5}
    """)})
    settle("HOUR")
    w = window_start("HOUR")
    L = (5, "HOUR")
    k = lambda v: f"{domain}_leaf_{v}_{w}"  # noqa: E731

    steps = [
        # request-level addend applies to descriptors without their own
        ({"hitsAddend": 2, "descriptors": [d(("leaf", "a"))]}, "OK", [ok(L, 3)], {k("a"): 2}),
        # a request-level addend of 0 still counts one hit
        ({"hitsAddend": 0, "descriptors": [d(("leaf", "a"))]}, "OK", [ok(L, 2)], {k("a"): 3}),
        # a descriptor-level addend overrides the request-level one
        ({"hitsAddend": 4, "descriptors": [d(("leaf", "a"), hitsAddend=1)]}, "OK", [ok(L, 1)], {k("a"): 4}),
        # descriptor-level 0 checks without counting, even on a new counter
        ({"hitsAddend": 3, "descriptors": [d(("leaf", "a"), hitsAddend=0), d(("leaf", "b"), hitsAddend=0)]},
         "OK", [ok(L, 1), ok(L, 5)], {k("a"): 4, k("b"): 0}),
        # one oversized addend is rejected and counted in full
        ({"hitsAddend": 6, "descriptors": [d(("leaf", "c"))]}, "OVER_LIMIT", [over(L)], {k("a"): 4, k("b"): 0, k("c"): 6}),
        # reaching the limit exactly is still allowed
        ({"hitsAddend": 2, "descriptors": [d(("leaf", "a"), hitsAddend="1"), d(("leaf", "e"))]},
         "OK", [ok(L, 0), ok(L, 3)], {k("a"): 5, k("b"): 0, k("c"): 6, k("e"): 2}),
    ]
    for body, overall, statuses, state in steps:
        reply, span = server.post({"domain": domain, **body})
        check_response(reply, span, overall, statuses)
        assert counters(valkey["main"], domain) == state, body


def test_negative_hits_refund(valkey, launch, domain):
    server = launch({"config/limits.yaml": config(domain, """
        - key: leaf
          rate_limit: {unit: hour, requests_per_unit: 5}
    """)})
    settle("HOUR")
    w = window_start("HOUR")
    L = (5, "HOUR")
    a, b = f"{domain}_leaf_a_{w}", f"{domain}_leaf_b_{w}"
    steps = [
        # refunding an absent counter reports full capacity and creates nothing
        ([d(("leaf", "a"), isNegativeHits=True)], {}, "OK", [ok(L, 5)], {}),
        ([d(("leaf", "a"), hitsAddend=4)], {}, "OK", [ok(L, 1)], {a: 4}),
        ([d(("leaf", "a"), isNegativeHits=True, hitsAddend=2)], {}, "OK", [ok(L, 3)], {a: 2}),
        # refunds floor at zero and keep the key
        ([d(("leaf", "a"), is_negative_hits=True, hitsAddend=9)], {}, "OK", [ok(L, 5)], {a: 0}),
        ([d(("leaf", "a"), isNegativeHits=True)], {"hitsAddend": 3}, "OK", [ok(L, 5)], {a: 0}),
        ([d(("leaf", "b"), hitsAddend=8)], {}, "OVER_LIMIT", [over(L)], {a: 0, b: 8}),
        # a refund is always OK, even when the counter stays above the limit
        ([d(("leaf", "b"), isNegativeHits=True)], {}, "OK", [ok(L, 0)], {a: 0, b: 7}),
        ([d(("leaf", "b"), isNegativeHits=True, hitsAddend=4)], {}, "OK", [ok(L, 2)], {a: 0, b: 3}),
    ]
    for descriptors, extra, overall, statuses, state in steps:
        reply, span = server.post({"domain": domain, **extra, "descriptors": descriptors})
        check_response(reply, span, overall, statuses)
        assert counters(valkey["main"], domain) == state, descriptors


def test_repeated_descriptor_counts_in_order(valkey, launch, domain):
    server = launch({"config/limits.yaml": config(domain, """
        - key: u
          rate_limit: {unit: hour, requests_per_unit: 3, name: urule}
    """)})
    settle("HOUR")
    L = (3, "HOUR")
    reply, span = server.post({"domain": domain, "descriptors": [d(("u", "x"))] * 4})
    check_response(reply, span, "OVER_LIMIT", [ok(L, 2, "urule"), ok(L, 1, "urule"), ok(L, 0, "urule"), over(L, "urule")])
    assert counters(valkey["main"], domain) == {f"{domain}_u_x_{window_start('HOUR')}": 4}


def test_cache_key_prefix(valkey, launch, domain, key_prefixes):
    prefix = f"p{domain}:"
    key_prefixes.append(prefix)
    server = launch({"config/limits.yaml": config(domain, """
        - key: k
          rate_limit: {unit: hour, requests_per_unit: 4}
    """)}, CACHE_KEY_PREFIX=prefix)
    settle("HOUR")
    key = f"{prefix}{domain}_k_v_{window_start('HOUR')}"
    L = (4, "HOUR")
    reply, span = server.post({"domain": domain, "descriptors": [d(("k", "v"), hitsAddend=3)]})
    check_response(reply, span, "OK", [ok(L, 1)])
    assert counters(valkey["main"], prefix) == {key: 3}
    reply, span = server.post({"domain": domain, "descriptors": [d(("k", "v"), isNegativeHits=True)]})
    check_response(reply, span, "OK", [ok(L, 2)])
    assert counters(valkey["main"], prefix) == {key: 2}
    assert counters(valkey["main"], domain) == {}
    assert 3597 <= ttl(valkey["main"], key) <= 3600


@pytest.mark.parametrize("jitter", [None, "5"], ids=["default", "explicit"])
def test_expiration_jitter(valkey, launch, domain, jitter):
    server = launch({"config/limits.yaml": config(domain, """
        - key: j
          rate_limit: {unit: minute, requests_per_unit: 9}
    """)}, EXPIRATION_JITTER_MAX_SECONDS=jitter)
    settle("MINUTE", margin=10)
    values = [str(i) for i in range(8)]
    reply, span = server.post({"domain": domain, "descriptors": [d(("j", v)) for v in values]})
    check_response(reply, span, "OK", [ok((9, "MINUTE"), 8)] * len(values))
    w = window_start("MINUTE")
    ttls = [ttl(valkey["main"], f"{domain}_j_{v}_{w}") for v in values]
    if jitter is None:
        # Default: up to 300 extra random seconds on top of the unit length.
        assert all(57 <= t <= 360 for t in ttls), ttls
        assert max(ttls) > 65, ttls
    else:
        assert all(57 <= t <= 65 for t in ttls), ttls


def test_per_second_split(valkey, launch, domain):
    server = launch({"config/limits.yaml": config(domain, """
        - key: burst
          rate_limit: {unit: second, requests_per_unit: 2}
        - key: h
          rate_limit: {unit: hour, requests_per_unit: 2}
        - key: m
          rate_limit: {unit: minute, requests_per_unit: 5}
    """)}, REDIS_PERSECOND="true", REDIS_PERSECOND_SOCKET_TYPE="tcp", REDIS_PERSECOND_URL=valkey["persec"])
    settle("MINUTE", "HOUR", margin=10)
    B = (2, "SECOND")
    reply, span = server.post({"domain": domain, "descriptors": [d(("burst", "b")), d(("h", "x")), d(("burst", "b")), d(("m", "y")), d(("burst", "b"))]})
    persec = counters(valkey["persec"], domain)
    check_response(reply, span, "OVER_LIMIT", [ok(B, 1), ok((2, "HOUR"), 1), ok(B, 0), ok((5, "MINUTE"), 4), over(B)])
    assert len(persec) == 1 and all(k.startswith(f"{domain}_burst_b_") for k in persec), persec
    assert list(persec.values()) == [3]
    assert ttl(valkey["persec"], next(iter(persec))) in (-2, 0, 1)  # expires after one second (-2: already gone)
    assert int(span.start) <= int(next(iter(persec)).rsplit("_", 1)[1]) <= int(span.end)
    assert counters(valkey["main"], domain) == {f"{domain}_h_x_{window_start('HOUR')}": 1, f"{domain}_m_y_{window_start('MINUTE')}": 1}


def test_stop_increment_when_over_limit(valkey, launch, domain):
    server = launch({"config/limits.yaml": config(domain, """
        - key: a
          rate_limit: {unit: hour, requests_per_unit: 2}
        - key: b
          rate_limit: {unit: hour, requests_per_unit: 10}
        - key: sh
          shadow_mode: true
          rate_limit: {unit: hour, requests_per_unit: 1}
    """)}, STOP_CACHE_KEY_INCREMENT_WHEN_OVERLIMIT="true")
    settle("HOUR")
    w = window_start("HOUR")
    k = lambda key, v: f"{domain}_{key}_{v}_{w}"  # noqa: E731
    A, Bl, SH = (2, "HOUR"), (10, "HOUR"), (1, "HOUR")
    steps = [
        ([d(("a", "x")), d(("b", "y"))], "OK", [ok(A, 1), ok(Bl, 9)], {k("a", "x"): 1, k("b", "y"): 1}),
        ([d(("a", "x")), d(("b", "y"))], "OK", [ok(A, 0), ok(Bl, 8)], {k("a", "x"): 2, k("b", "y"): 2}),
        # a would exceed: only a is counted, b is left as it was
        ([d(("a", "x")), d(("b", "y"))], "OVER_LIMIT", [over(A), ok(Bl, 8)], {k("a", "x"): 3, k("b", "y"): 2}),
        ([d(("b", "y"), hitsAddend=20), d(("a", "q"))], "OVER_LIMIT", [over(Bl), ok(A, 2)],
         {k("a", "x"): 3, k("b", "y"): 22, k("a", "q"): 0}),
        # a shadow-mode rule that would exceed also holds back the others
        ([d(("sh", "s"), hitsAddend=2), d(("b", "z"))], "OK", [ok(SH, 0), ok(Bl, 10)],
         {k("a", "x"): 3, k("b", "y"): 22, k("a", "q"): 0, k("sh", "s"): 2, k("b", "z"): 0}),
        # the decision uses the counts from before the request
        ([d(("a", "w"))] * 3, "OVER_LIMIT", [ok(A, 1), ok(A, 0), over(A)],
         {k("a", "x"): 3, k("b", "y"): 22, k("a", "q"): 0, k("sh", "s"): 2, k("b", "z"): 0, k("a", "w"): 3}),
        # refunds are applied regardless
        ([d(("a", "x")), d(("b", "y"), isNegativeHits=True, hitsAddend=2)], "OVER_LIMIT", [over(A), ok(Bl, 0)],
         {k("a", "x"): 4, k("b", "y"): 20, k("a", "q"): 0, k("sh", "s"): 2, k("b", "z"): 0, k("a", "w"): 3}),
    ]
    for descriptors, overall, statuses, state in steps:
        reply, span = server.post({"domain": domain, "descriptors": descriptors})
        check_response(reply, span, overall, statuses)
        assert counters(valkey["main"], domain) == state, descriptors
