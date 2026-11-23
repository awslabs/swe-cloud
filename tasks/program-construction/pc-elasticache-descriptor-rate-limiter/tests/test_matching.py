"""Descriptor matching and the rule options of the configuration language."""

from support import (
    OK_BARE,
    UNLIMITED,
    config,
    counters,
    check_response,
    d,
    ok,
    over,
    settle,
    window_start,
)


def test_exact_value_then_wildcard_then_key_only(valkey, launch, domain):
    rules = """
        - key: path
          rate_limit: {unit: hour, requests_per_unit: 10}
        - key: path
          value: /api/*
          rate_limit: {unit: hour, requests_per_unit: 20, name: apiwild}
        - key: path
          value: /api/v1/*
          rate_limit: {unit: hour, requests_per_unit: 30}
        - key: path
          value: /api/special
          rate_limit: {unit: hour, requests_per_unit: 40}
    """
    reordered = """
        - key: path
          value: /api/v1/*
          rate_limit: {unit: hour, requests_per_unit: 30}
        - key: path
          value: /api/*
          rate_limit: {unit: hour, requests_per_unit: 20}
    """
    other = domain + "r"
    server = launch({"config/first.yaml": config(domain, rules), "config/second.yaml": config(other, reordered)})
    settle("HOUR")
    w = window_start("HOUR")
    H = lambda n: (n, "HOUR")  # noqa: E731
    reply, span = server.post({"domain": domain, "descriptors": [
        d(("path", "/api/special")), d(("path", "/api/v1/x")), d(("path", "/api/x")),
        d(("path", "/other")), d(("path", "")), {"entries": [{"key": "path"}]},
    ]})
    check_response(reply, span, "OK", [
        ok(H(40), 39), ok(H(20), 19, "apiwild"), ok(H(20), 19, "apiwild"), ok(H(10), 9), ok(H(10), 9), ok(H(10), 8),
    ])
    # Counters are keyed by the request's own values, not the matched pattern.
    assert counters(valkey["main"], domain + "_") == {
        f"{domain}_path_/api/special_{w}": 1,
        f"{domain}_path_/api/v1/x_{w}": 1,
        f"{domain}_path_/api/x_{w}": 1,
        f"{domain}_path_/other_{w}": 1,
        f"{domain}_path__{w}": 2,
    }
    # Among wildcards the first listed pattern wins; without a key-only rule nothing else matches.
    reply, span = server.post({"domain": other, "descriptors": [d(("path", "/api/v1/x")), d(("path", "/api/x")), d(("path", "/other"))]})
    check_response(reply, span, "OK", [ok(H(30), 29), ok(H(20), 19), OK_BARE])
    assert counters(valkey["main"], other + "_") == {f"{other}_path_/api/v1/x_{w}": 1, f"{other}_path_/api/x_{w}": 1}


def test_wildcard_patterns(valkey, launch, domain):
    server = launch({"config/limits.yaml": config(domain, """
        - key: file
          value: a*z
          rate_limit: {unit: hour, requests_per_unit: 2}
        - key: pre
          value: ab*
          rate_limit: {unit: hour, requests_per_unit: 8}
        - key: star
          value: "*"
          rate_limit: {unit: hour, requests_per_unit: 4}
        - key: star
          rate_limit: {unit: hour, requests_per_unit: 5}
    """)})
    settle("HOUR")
    w = window_start("HOUR")
    reply, span = server.post({"domain": domain, "descriptors": [
        d(("file", "az")), d(("file", "abcz")), d(("file", "abc")), d(("file", "za")),
        d(("pre", "ab")), d(("pre", "abc d_é")), d(("pre", "a")),
        d(("star", "")), d(("star", "anything")), {"entries": [{"key": "star"}]},
    ]})
    F, P, ST = (2, "HOUR"), (8, "HOUR"), (4, "HOUR")
    check_response(reply, span, "OK", [
        ok(F, 1), ok(F, 1), OK_BARE, OK_BARE,
        ok(P, 7), ok(P, 7), OK_BARE,
        ok(ST, 3), ok(ST, 3), ok(ST, 2),
    ])
    assert counters(valkey["main"], domain) == {
        f"{domain}_file_az_{w}": 1,
        f"{domain}_file_abcz_{w}": 1,
        f"{domain}_pre_ab_{w}": 1,
        f"{domain}_pre_abc d_é_{w}": 1,
        f"{domain}_star__{w}": 2,
        f"{domain}_star_anything_{w}": 1,
    }


def test_nested_descriptors_match_whole_entry_lists(valkey, launch, domain):
    server = launch({"config/limits.yaml": config(domain, """
        - key: user
          rate_limit: {unit: hour, requests_per_unit: 3, name: peruser}
          descriptors:
            - key: path
              value: /admin
              rate_limit: {unit: hour, requests_per_unit: 1}
        - key: leaf
          rate_limit: {unit: hour, requests_per_unit: 5}
        - key: parent
          descriptors:
            - key: child
              value: c1
              rate_limit: {unit: day, requests_per_unit: 6}
            - key: child
              descriptors:
                - key: leaf
                  rate_limit: {unit: minute, requests_per_unit: 7}
    """)})
    settle("MINUTE", "HOUR", "DAY", margin=15)
    reply, span = server.post({"domain": domain, "descriptors": [
        d(("user", "u1"), ("path", "/x")),
        d(("leaf", "l"), ("more", "m")),
        d(("user", "u1"), ("path", "/admin")),
        d(("user", "u1")),
        d(("parent", "p"), ("child", "c1")),
        d(("parent", "p"), ("child", "c2"), ("leaf", "l")),
        d(("parent", "p"), ("child", "c1"), ("leaf", "l")),
        d(("parent", "p")),
        d(("parent", "p"), ("child", "c2")),
    ]})
    check_response(reply, span, "OK", [
        OK_BARE, OK_BARE, ok((1, "HOUR"), 0), ok((3, "HOUR"), 2, "peruser"),
        ok((6, "DAY"), 5), ok((7, "MINUTE"), 6), OK_BARE, OK_BARE, OK_BARE,
    ])
    assert counters(valkey["main"], domain) == {
        f"{domain}_user_u1_path_/admin_{window_start('HOUR')}": 1,
        f"{domain}_user_u1_{window_start('HOUR')}": 1,
        f"{domain}_parent_p_child_c1_{window_start('DAY')}": 1,
        f"{domain}_parent_p_child_c2_leaf_l_{window_start('MINUTE')}": 1,
    }


def test_replaces_by_rule_name(valkey, launch, domain):
    server = launch({"config/limits.yaml": config(domain, """
        - key: user
          rate_limit: {unit: hour, requests_per_unit: 3, name: peruser}
        - key: tier
          value: gold
          descriptors:
            - key: user
              rate_limit:
                unit: hour
                requests_per_unit: 100
                replaces:
                  - name: peruser
        - key: path
          value: /api/*
          rate_limit: {unit: hour, requests_per_unit: 20, name: apiwild}
        - key: free
          rate_limit: {unlimited: true, replaces: [{name: apiwild}]}
    """)})
    settle("HOUR")
    w = window_start("HOUR")
    U, G = (3, "HOUR"), (100, "HOUR")
    gold, plain = f"{domain}_tier_gold_user_u2_{w}", f"{domain}_user_u2_{w}"
    steps = [
        ([d(("user", "u2")), d(("tier", "gold"), ("user", "u2"))], [OK_BARE, ok(G, 99)], {gold: 1}),
        # replacement only happens inside one request
        ([d(("user", "u2"))], [ok(U, 2, "peruser")], {gold: 1, plain: 1}),
        # and does not depend on descriptor order
        ([d(("tier", "gold"), ("user", "u2")), d(("user", "u2"))], [ok(G, 98), OK_BARE], {gold: 2, plain: 1}),
        # an unlimited rule can replace too
        ([d(("path", "/api/zz")), d(("free", "f"))], [OK_BARE, UNLIMITED], {gold: 2, plain: 1}),
        ([d(("path", "/api/zz"))], [ok((20, "HOUR"), 19, "apiwild")], {gold: 2, plain: 1, f"{domain}_path_/api/zz_{w}": 1}),
    ]
    for descriptors, statuses, state in steps:
        reply, span = server.post({"domain": domain, "descriptors": descriptors})
        check_response(reply, span, "OK", statuses)
        assert counters(valkey["main"], domain) == state, descriptors


def test_unlimited_rules(valkey, launch, domain):
    server = launch({"config/limits.yaml": config(domain, """
        - key: free
          rate_limit: {unlimited: true}
        - key: k
          rate_limit: {unit: hour, requests_per_unit: 2}
    """)})
    settle("HOUR")
    key = f"{domain}_k_b_{window_start('HOUR')}"
    reply, span = server.post({"domain": domain, "descriptors": [d(("free", "a")), d(("k", "b")), d(("free", "a"), hitsAddend=50)]})
    check_response(reply, span, "OK", [UNLIMITED, ok((2, "HOUR"), 1), UNLIMITED])
    assert counters(valkey["main"], domain) == {key: 1}
    reply, span = server.post({"domain": domain, "descriptors": [d(("k", "b"), hitsAddend=5), d(("free", "a"), isNegativeHits=True)]})
    check_response(reply, span, "OVER_LIMIT", [over((2, "HOUR")), UNLIMITED])
    assert counters(valkey["main"], domain) == {key: 6}


def test_shadow_mode(valkey, launch, domain):
    server = launch({"config/limits.yaml": config(domain, """
        - key: shadow
          shadow_mode: true
          rate_limit: {unit: hour, requests_per_unit: 1}
        - key: leaf
          rate_limit: {unit: hour, requests_per_unit: 5}
    """)})
    settle("HOUR")
    w = window_start("HOUR")
    S1, L = (1, "HOUR"), (5, "HOUR")
    sk, lk = f"{domain}_shadow_s_{w}", f"{domain}_leaf_l_{w}"
    steps = [
        ([d(("shadow", "s"), hitsAddend=3), d(("leaf", "l"))], "OK", [ok(S1, 0), ok(L, 4)], {sk: 3, lk: 1}),
        ([d(("shadow", "s")), d(("shadow", "s"))], "OK", [ok(S1, 0), ok(S1, 0)], {sk: 5, lk: 1}),
        # shadow mode only masks its own rule
        ([d(("shadow", "s")), d(("leaf", "l"), hitsAddend=10)], "OVER_LIMIT", [ok(S1, 0), over(L)], {sk: 6, lk: 11}),
    ]
    for descriptors, overall, statuses, state in steps:
        reply, span = server.post({"domain": domain, "descriptors": descriptors})
        check_response(reply, span, overall, statuses)
        assert counters(valkey["main"], domain) == state, descriptors


def test_detailed_metric_does_not_change_limiting(valkey, launch, domain):
    server = launch({"config/limits.yaml": config(domain, """
        - key: dm
          detailed_metric: true
          rate_limit: {unit: day, requests_per_unit: 2}
        - key: plain
          rate_limit: {unit: day, requests_per_unit: 2}
    """)})
    settle("DAY")
    D2 = (2, "DAY")
    for overall, status in (("OK", ok(D2, 1)), ("OK", ok(D2, 0)), ("OVER_LIMIT", over(D2))):
        reply, span = server.post({"domain": domain, "descriptors": [d(("dm", "x")), d(("plain", "x"))]})
        check_response(reply, span, overall, [status, status])
    w = window_start("DAY")
    assert counters(valkey["main"], domain) == {f"{domain}_dm_x_{w}": 3, f"{domain}_plain_x_{w}": 3}


def test_rule_names_and_unit_spelling(valkey, launch, domain):
    server = launch({"config/limits.yaml": config(domain, """
        - key: a
          rate_limit: {unit: Hour, requests_per_unit: 2, name: capsrule}
        - key: b
          rate_limit: {unit: minute, requests_per_unit: 3}
        - key: c
          rate_limit: {unit: DAY, requests_per_unit: 4, name: "day rule"}
    """)})
    settle("MINUTE", "HOUR", "DAY", margin=10)
    reply, span = server.post({"domain": domain, "descriptors": [d(("a", "1")), d(("b", "2")), d(("c", "3"))]})
    check_response(reply, span, "OK", [ok((2, "HOUR"), 1, "capsrule"), ok((3, "MINUTE"), 2), ok((4, "DAY"), 3, "day rule")])
    assert counters(valkey["main"], domain) == {
        f"{domain}_a_1_{window_start('HOUR')}": 1,
        f"{domain}_b_2_{window_start('MINUTE')}": 1,
        f"{domain}_c_3_{window_start('DAY')}": 1,
    }


def test_zero_requests_per_unit(valkey, launch, domain):
    server = launch({"config/limits.yaml": config(domain, """
        - key: zero
          rate_limit: {unit: hour, requests_per_unit: 0}
    """)})
    settle("HOUR")
    w = window_start("HOUR")
    Z = (0, "HOUR")
    reply, span = server.post({"domain": domain, "descriptors": [d(("zero", "y"), hitsAddend=0)]})
    check_response(reply, span, "OK", [ok(Z, 0)])
    reply, span = server.post({"domain": domain, "descriptors": [d(("zero", "z")), d(("zero", "z"), hitsAddend=0)]})
    check_response(reply, span, "OVER_LIMIT", [over(Z), over(Z)])
    assert counters(valkey["main"], domain) == {f"{domain}_zero_y_{w}": 0, f"{domain}_zero_z_{w}": 1}
