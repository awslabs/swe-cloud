"""Single-flag OFREP evaluation: variations, rule precedence, splits, bucketing and invalid flags.

Expectations are native observations (curation.md: experiments/scen_eval.py, scen_query.py,
scen_more.py). Some flags are adapted from upstream modules/core/flag/internal_flag_test.go.
"""

import pytest

from relaykit import assert_response

CACHEABLE = {"gofeatureflag_cacheable": True}

FLAGS = {
    "static-num": {"variations": {"low": 10, "high": 1000.5}, "defaultRule": {"variation": "high"},
                   "version": "1.2", "metadata": {"owner": "team-a", "n": 3}},
    "negative": {"variations": {"a": -1.5e3, "b": 0}, "defaultRule": {"variation": "a"}},
    "whole-float": {"variations": {"one": 1.0, "big": 1e3}, "defaultRule": {"variation": "one"}},
    "bool": {"variations": {"on": True, "off": False}, "defaultRule": {"variation": "off"}},
    "obj": {"variations": {"x": {"nested": {"list": [1, "two", None, True]}}, "y": {}}, "defaultRule": {"variation": "x"}},
    "rules": {"variations": {"a": "A", "b": "B", "c": "C", "d": "D"},
              "targeting": [{"name": "r-off", "query": "plan eq \"pro\"", "variation": "d", "disable": True},
                            {"name": "r1", "query": "plan eq \"pro\"", "variation": "a"},
                            {"query": "plan in [\"pro\", \"team\"]", "variation": "b"}],
              "defaultRule": {"name": "dflt", "variation": "c"}},
    "rule-split": {"variations": {"x": [1, 2], "y": [3], "z": []},
                   "targeting": [{"name": "s", "query": "beta eq true", "percentage": {"x": 50, "y": 50}},
                                 {"name": "full", "query": "beta eq false", "percentage": {"x": 0, "y": 100}}],
                   "defaultRule": {"variation": "z"}},
    "dflt-split": {"variations": {"on": {"k": 1}, "off": {"k": 0}}, "defaultRule": {"percentage": {"on": 30, "off": 70}}},
    "dflt-100": {"variations": {"on": True, "off": False}, "defaultRule": {"percentage": {"on": 100, "off": 0}}},
    "both": {"variations": {"a": "A", "b": "B", "c": "C"},
             "targeting": [{"name": "r", "query": "x eq 1", "variation": "a", "percentage": {"a": 0, "b": 100}}],
             "defaultRule": {"variation": "c", "percentage": {"c": 0, "a": 100}}},
    "dflt-disabled": {"variations": {"a": "A", "b": "B"}, "defaultRule": {"variation": "b", "disable": True}},
    "killed": {"variations": {"a": 1, "b": 2}, "defaultRule": {"variation": "a"}, "disable": True, "metadata": {"m": "x"}},
    "killed-split": {"variations": {"a": 1, "b": 2}, "defaultRule": {"percentage": {"a": 50, "b": 50}}, "disable": True},
    "pct3": {"variations": {"alpha": 1, "beta": 2, "gamma": 3},
             "defaultRule": {"percentage": {"alpha": 20, "beta": 30.5, "gamma": 49.5}}},
    "pct2": {"variations": {"True": True, "False": False}, "defaultRule": {"percentage": {"True": 30, "False": 70}}},
    "pct60": {"variations": {"a": "A", "b": "B"}, "defaultRule": {"percentage": {"a": 30, "b": 30}}},
    "bkey": {"variations": {"a": "A", "b": "B"}, "bucketingKey": "teamId", "defaultRule": {"percentage": {"a": 50, "b": 50}}},
    "bkey-nested": {"variations": {"a": "A", "b": "B"}, "bucketingKey": "team.id",
                    "defaultRule": {"percentage": {"a": 50, "b": 50}}},
    "bkey-static": {"variations": {"a": "A", "b": "B"}, "bucketingKey": "teamId", "defaultRule": {"variation": "b"}},
    # Invalid definitions: never become flags.
    "bad-query": {"variations": {"a": "A", "b": "B"}, "targeting": [{"query": "email eq", "variation": "a"}], "defaultRule": {"variation": "b"}},
    "mixed-types": {"variations": {"a": "A", "b": 1}, "defaultRule": {"variation": "a"}},
    "no-default": {"variations": {"a": "A", "b": "B"}, "targeting": [{"query": "x eq 1", "variation": "a"}]},
    "unknown-variation": {"variations": {"a": "A"}, "defaultRule": {"variation": "zzz"}},
    "duplicate-rule-names": {"variations": {"a": "A", "b": "B"},
                             "targeting": [{"name": "x", "query": "k eq 1", "variation": "a"}, {"name": "x", "query": "k eq 2", "variation": "b"}],
                             "defaultRule": {"variation": "a"}},
    "rule-without-query": {"variations": {"a": "A", "b": "B"}, "targeting": [{"name": "x", "variation": "a"}], "defaultRule": {"variation": "b"}},
    "pct-unknown-variation": {"variations": {"a": "A", "b": "B"}, "defaultRule": {"percentage": {"a": 50, "zz": 50}}},
    "no-variations": {"variations": {}, "defaultRule": {"variation": "a"}},
}


def ok(key, value, variant, reason, metadata=None):
    return 200, {"key": key, "value": value, "variant": variant, "reason": reason, "metadata": {**(metadata or {}), **CACHEABLE}}


def error(key, code, status=400):
    return status, {"key": key, "errorCode": code, "errorDetails": "*"}


@pytest.mark.parametrize(("key", "expected"), [
    ("static-num", ok("static-num", 1000.5, "high", "STATIC", {"owner": "team-a", "n": 3})),
    ("negative", ok("negative", -1500, "a", "STATIC")),
    ("whole-float", ok("whole-float", 1, "one", "STATIC")),
    ("bool", ok("bool", False, "off", "STATIC")),
    ("obj", ok("obj", {"nested": {"list": [1, "two", None, True]}}, "x", "STATIC")),
])
def test_static_values_keep_their_json_kind(module_relay, key, expected):
    assert_response(module_relay.ofrep(key, {"targetingKey": "u-1"}), *expected)


@pytest.mark.parametrize(("context", "expected"), [
    ({"targetingKey": "u-1", "plan": "pro"}, ok("rules", "A", "a", "TARGETING_MATCH", {"evaluatedRuleName": "r1"})),
    ({"targetingKey": "u-2", "plan": "team"}, ok("rules", "B", "b", "TARGETING_MATCH")),
    ({"targetingKey": "u-3", "plan": "free"}, ok("rules", "C", "c", "DEFAULT")),
    ({"targetingKey": "u-4"}, ok("rules", "C", "c", "DEFAULT")),
    ({"plan": "pro"}, ok("rules", "A", "a", "TARGETING_MATCH", {"evaluatedRuleName": "r1"})),
])
def test_rules_in_order_skip_disabled_and_fall_back_to_default(module_relay, context, expected):
    assert_response(module_relay.ofrep("rules", context), *expected)


@pytest.mark.parametrize(("key", "context", "expected"), [
    ("rule-split", {"targetingKey": "u-1", "beta": True}, ok("rule-split", [3], "y", "TARGETING_MATCH_SPLIT", {"evaluatedRuleName": "s"})),
    ("rule-split", {"targetingKey": "u-4", "beta": False}, ok("rule-split", [3], "y", "TARGETING_MATCH", {"evaluatedRuleName": "full"})),
    ("rule-split", {"targetingKey": "u-5"}, ok("rule-split", [], "z", "DEFAULT")),
    ("dflt-split", {"targetingKey": "u-1"}, ok("dflt-split", {"k": 0}, "off", "SPLIT")),
    ("dflt-100", {"targetingKey": "u-1"}, ok("dflt-100", True, "on", "STATIC")),
    ("both", {"targetingKey": "u1", "x": 1}, ok("both", "B", "b", "TARGETING_MATCH", {"evaluatedRuleName": "r"})),
    ("both", {"targetingKey": "u1", "x": 2}, ok("both", "A", "a", "DEFAULT")),
    ("dflt-disabled", {"targetingKey": "u1"}, ok("dflt-disabled", "B", "b", "STATIC")),
])
def test_split_reasons_and_rule_field_precedence(module_relay, key, context, expected):
    assert_response(module_relay.ofrep(key, context), *expected)


# Native assignments of targeting keys key-0 .. key-19 (experiments/scen_query.py "B" lines).
BUCKETS = {
    "pct3": ["beta", "alpha", "gamma", "gamma", "beta", "alpha", "gamma", "gamma", "alpha", "gamma",
             "beta", "beta", "gamma", "alpha", "alpha", "beta", "gamma", "gamma", "alpha", "beta"],
    "pct2": ["False", "False", "True", "True", "False", "False", "False", "True", "False", "False",
             "False", "False", "True", "False", "False", "False", "True", "False", "False", "True"],
    "pct60": ["a", "b", "a", "a", "b", "a", "b", "b", "a", "b", "a", "a", "b", "a", "b", "b", "a", "b", "a", "a"],
}
VALUES = {"pct3": {"alpha": 1, "beta": 2, "gamma": 3}, "pct2": {"True": True, "False": False}, "pct60": {"a": "A", "b": "B"}}


@pytest.mark.parametrize("key", sorted(BUCKETS))
def test_percentage_bucket_assignment(module_relay, key):
    for i, variant in enumerate(BUCKETS[key]):
        assert_response(module_relay.ofrep(key, {"targetingKey": f"key-{i}"}),
                        *ok(key, VALUES[key][variant], variant, "SPLIT"), label=f"key-{i}")


# Native: experiments/scen_more.py "bkey"/"bkeyn" lines; identical for targeting keys A and B.
TEAM_BUCKETS = {"bkey": "bbaabbba", "bkey-nested": "aaabaabb"}


@pytest.mark.parametrize("key", sorted(TEAM_BUCKETS))
def test_bucketing_key_attribute_replaces_targeting_key(module_relay, key):
    for i, variant in enumerate(TEAM_BUCKETS[key], start=1):
        team = f"t-{i}"
        for subject in ("A", "B"):
            context = {"targetingKey": subject, **({"teamId": team} if key == "bkey" else {"team": {"id": team}})}
            assert_response(module_relay.ofrep(key, context), *ok(key, variant.upper(), variant, "SPLIT"), label=f"{subject}/{team}")
    # The attribute alone is enough; the targeting key is then not needed.
    assert_response(module_relay.ofrep("bkey", {"teamId": "t-1"}), *ok("bkey", "B", "b", "SPLIT"))


@pytest.mark.parametrize(("key", "context", "expected"), [
    ("bkey", {"targetingKey": "u-1"}, error("bkey", "TARGETING_KEY_MISSING")),
    ("bkey", {"targetingKey": "u-1", "teamId": 5}, error("bkey", "TARGETING_KEY_MISSING")),
    ("bkey", {"targetingKey": "u-1", "teamId": ""}, error("bkey", "TARGETING_KEY_MISSING")),
    ("bkey-static", {"targetingKey": "u-1"}, error("bkey-static", "TARGETING_KEY_MISSING")),
    ("bkey-static", {"targetingKey": "u-1", "teamId": ""}, ok("bkey-static", "B", "b", "STATIC")),
    ("bkey-static", {"teamId": "t"}, ok("bkey-static", "B", "b", "STATIC")),
])
def test_bucketing_key_attribute_errors(module_relay, key, context, expected):
    assert_response(module_relay.ofrep(key, context), *expected)


@pytest.mark.parametrize("context", [{"plan": "pro"}, {"targetingKey": "", "plan": "pro"}, {"targetingKey": 12, "plan": "pro"}])
def test_missing_targeting_key_only_fails_flags_that_split(module_relay, context):
    assert_response(module_relay.ofrep("static-num", context), *ok("static-num", 1000.5, "high", "STATIC", {"owner": "team-a", "n": 3}))
    assert_response(module_relay.ofrep("rules", context), *ok("rules", "A", "a", "TARGETING_MATCH", {"evaluatedRuleName": "r1"}))
    assert_response(module_relay.ofrep("killed", context), *ok("killed", None, "SdkDefault", "DISABLED", {"m": "x"}))
    for key in ("rule-split", "dflt-split", "dflt-100", "killed-split"):
        assert_response(module_relay.ofrep(key, context), *error(key, "TARGETING_KEY_MISSING"), label=key)


def test_disabled_flags(module_relay):
    assert_response(module_relay.ofrep("killed", {"targetingKey": "u-1"}), *ok("killed", None, "SdkDefault", "DISABLED", {"m": "x"}))
    assert_response(module_relay.ofrep("killed-split", {"targetingKey": "u-1"}), *ok("killed-split", None, "SdkDefault", "DISABLED"))


@pytest.mark.parametrize("key", ["bad-query", "mixed-types", "no-default", "unknown-variation", "duplicate-rule-names",
                                 "rule-without-query", "pct-unknown-variation", "no-variations", "never-stored"])
def test_invalid_or_missing_definitions_are_not_found(module_relay, key):
    assert_response(module_relay.ofrep(key, {"targetingKey": "u-1", "x": 1}), *error(key, "FLAG_NOT_FOUND", 404))
