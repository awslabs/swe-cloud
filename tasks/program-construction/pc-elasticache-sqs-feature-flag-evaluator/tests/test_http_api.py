"""HTTP request handling and response shapes of the OFREP and native endpoints.

The flag set adapts upstream cmd/relayproxy/testdata/goff/config_flags.yaml (converted to
fixtures/upstream_ofrep_flags.json) with the request contexts of cmd/relayproxy/testdata/ofrep/*.json.
Expectations are native observations (experiments/scen_eval.py, scen_life2.py, scen_more.py).
"""

import json
from pathlib import Path

import pytest

from relaykit import V1_UNGRADED, assert_allflags, assert_bulk, assert_json, assert_response

UPSTREAM = json.loads((Path(__file__).parent / "fixtures/upstream_ofrep_flags.json").read_text())
FLAGS = {
    **UPSTREAM,
    "ver": {"variations": {"a": "A"}, "defaultRule": {"variation": "a"}, "version": "7.1.0", "metadata": {"desc": "d", "issue": 12}},
    "notrack": {"variations": {"a": "A"}, "defaultRule": {"variation": "a"}, "trackEvents": False},
}

KEY = "4f433951-4c8c-42b3-9f18-8c9a5ed8e9eb"
VALID = {"company": "GO Feature Flag", "firstname": "John", "lastname": "Doe", "targetingKey": KEY}
NO_KEY = {k: v for k, v in VALID.items() if k != "targetingKey"}
DEFAULT_ARRAY = ["batmanDefault", "supermanDefault", "superherosDefault"]
OBJ = {"test": "test"}


def entry(key, value, variant, reason, metadata=None):
    out = {"key": key, "value": value, "variant": variant, "reason": reason}
    if metadata:
        out["metadata"] = metadata
    return out


def missing_key(key):
    return {"key": key, "value": None, "variant": "", "reason": "ERROR", "errorCode": "TARGETING_KEY_MISSING", "errorDetails": "*"}


VER = entry("ver", "A", "a", "STATIC", {"desc": "d", "issue": 12})
NOTRACK = entry("notrack", "A", "a", "STATIC")

BULK_VALID = [
    entry("array-flag", DEFAULT_ARRAY, "Default", "DEFAULT"),
    entry("disable-flag", None, "", "DISABLED"),
    entry("flag-only-for-admin", False, "Default", "DEFAULT"),
    entry("new-admin-access", True, "True", "SPLIT"),
    NOTRACK,
    entry("number-flag", 1, "Default", "DEFAULT"),
    entry("targeting-key-rule", False, "false_var", "DEFAULT"),
    entry("test-flag-rule-apply", OBJ, "Default", "DEFAULT"),
    entry("test-flag-rule-apply-false", OBJ, "Default", "DEFAULT"),
    entry("test-flag-rule-not-apply", OBJ, "Default", "DEFAULT"),
    VER,
]


def test_bulk_evaluates_every_flag_sorted_by_key(module_relay):
    assert_bulk(module_relay.bulk(VALID), BULK_VALID)


def test_bulk_without_targeting_key_reports_errors_inline(module_relay):
    assert_bulk(module_relay.bulk(NO_KEY), [
        missing_key("array-flag"), missing_key("disable-flag"), missing_key("flag-only-for-admin"),
        missing_key("new-admin-access"), NOTRACK, missing_key("number-flag"),
        entry("targeting-key-rule", False, "false_var", "DEFAULT"),
        missing_key("test-flag-rule-apply"), missing_key("test-flag-rule-apply-false"), missing_key("test-flag-rule-not-apply"),
        VER,
    ])


def test_bulk_reports_rule_names_without_cache_marker(module_relay):
    assert_bulk(module_relay.bulk({**VALID, "anonymous": True}), [
        entry("array-flag", ["batmanFalse", "supermanFalse", "superherosFalse"], "False", "TARGETING_MATCH_SPLIT", {"evaluatedRuleName": "rule1"}),
        entry("disable-flag", None, "", "DISABLED"),
        entry("flag-only-for-admin", False, "Default", "DEFAULT"),
        entry("new-admin-access", True, "True", "SPLIT"),
        NOTRACK,
        entry("number-flag", 2, "True", "TARGETING_MATCH", {"evaluatedRuleName": "rule1"}),
        entry("targeting-key-rule", False, "false_var", "DEFAULT"),
        entry("test-flag-rule-apply", OBJ, "Default", "DEFAULT"),
        entry("test-flag-rule-apply-false", {"test3": "test"}, "False", "TARGETING_MATCH_SPLIT", {"evaluatedRuleName": "rule1"}),
        entry("test-flag-rule-not-apply", OBJ, "Default", "DEFAULT"),
        VER,
    ])
    response = module_relay.bulk({**VALID, "admin": True})
    assert_json(response[1]["flags"][2], entry("flag-only-for-admin", True, "True", "TARGETING_MATCH", {"evaluatedRuleName": "rule1"}))


@pytest.mark.parametrize(("flag_list", "keys"), [
    (["array-flag", "flag-only-for-admin"], ["array-flag", "flag-only-for-admin"]),
    (["ver", "nope", "ver", "array-flag"], ["array-flag", "ver"]),
    (["nope", "nada"], []),
    ([], None),
    ("ver", None),
])
def test_bulk_flag_list_selection(module_relay, flag_list, keys):
    expected = [f for f in BULK_VALID if keys is None or f["key"] in keys]
    assert_bulk(module_relay.bulk({**VALID, "gofeatureflag": {"flagList": flag_list}}), expected)


@pytest.mark.parametrize(("raw", "with_key"), [
    (b"{}", True), (b'{"context": null}', True), (b"", True), (b"{nope", True), (b'{"context": [1]}', True),
    (b"{}", False), (b"{nope", False),
])
def test_ofrep_rejects_requests_without_a_context_object(module_relay, raw, with_key):
    path = "/ofrep/v1/evaluate/flags/number-flag" if with_key else "/ofrep/v1/evaluate/flags"
    body = {"errorCode": "INVALID_CONTEXT", "errorDetails": "*", **({"key": "number-flag"} if with_key else {})}
    assert_response(module_relay.request("POST", path, raw=raw), 400, body)


def test_ofrep_single_upstream_cases(module_relay):
    cacheable = {"gofeatureflag_cacheable": True}
    # Upstream responses/valid_evaluation.json, no_targeting_key_static_flag.json,
    # valid_targeting_key_query_response.json, no_targeting_key_bucketing_flag.json.
    assert_response(module_relay.ofrep("number-flag", VALID), 200, entry("number-flag", 1, "Default", "DEFAULT", cacheable))
    assert_response(module_relay.ofrep("targeting-key-rule", NO_KEY), 200,
                    entry("targeting-key-rule", False, "false_var", "DEFAULT", cacheable))
    assert_response(module_relay.ofrep("targeting-key-rule", {**VALID, "targetingKey": "specific-targeting-key"}), 200,
                    entry("targeting-key-rule", True, "true_var", "TARGETING_MATCH", cacheable))
    for key in ("number-flag", "flag-only-for-admin", "disable-flag"):
        assert_response(module_relay.ofrep(key, NO_KEY), 400, {"key": key, "errorCode": "TARGETING_KEY_MISSING", "errorDetails": "*"})
    assert_response(module_relay.ofrep("disable-flag", VALID), 200, entry("disable-flag", None, "SdkDefault", "DISABLED", cacheable))
    assert_response(module_relay.ofrep("test-flag-rule-apply", {"targetingKey": "random-key"}), 200,
                    entry("test-flag-rule-apply", {"test2": "test"}, "True", "TARGETING_MATCH", {"evaluatedRuleName": "rule1", **cacheable}))
    assert_response(module_relay.ofrep("number-flag", {**VALID, "anonymous": "true"}), 200,
                    entry("number-flag", 1, "Default", "DEFAULT", cacheable))
    assert_response(module_relay.ofrep("ver", {}), 200, entry("ver", "A", "a", "STATIC", {"desc": "d", "issue": 12, **cacheable}))


NATIVE_OK = {"errorCode": "", "failed": False, "trackEvents": True, "version": ""}


@pytest.mark.parametrize(("key", "body", "expected"), [
    ("ver", {"evaluationContext": {"key": "u-v"}, "defaultValue": "x"},
     {**NATIVE_OK, "value": "A", "variationType": "a", "reason": "STATIC", "version": "7.1.0", "metadata": {"desc": "d", "issue": 12}}),
    ("ver", {"evaluationContext": {"custom": {"a": 1}}, "defaultValue": [1]},
     {**NATIVE_OK, "value": "A", "variationType": "a", "reason": "STATIC", "version": "7.1.0", "metadata": {"desc": "d", "issue": 12}}),
    ("number-flag", {"evaluationContext": {"key": "u-a", "custom": {"anonymous": True}}, "defaultValue": 0},
     {**NATIVE_OK, "value": 2, "variationType": "True", "reason": "TARGETING_MATCH", "metadata": {"evaluatedRuleName": "rule1"}}),
    ("number-flag", {"evaluationContext": {"key": "u-a", "anonymous": True}, "defaultValue": 0},
     {**NATIVE_OK, "value": 1, "variationType": "Default", "reason": "DEFAULT"}),
    ("test-flag-rule-apply", {"evaluationContext": {"key": "random-key"}},
     {**NATIVE_OK, "value": {"test2": "test"}, "variationType": "True", "reason": "TARGETING_MATCH", "metadata": {"evaluatedRuleName": "rule1"}}),
    ("disable-flag", {"evaluationContext": {"key": "u-k"}, "defaultValue": 42},
     {**NATIVE_OK, "value": 42, "variationType": "SdkDefault", "reason": "DISABLED"}),
    ("disable-flag", {"evaluationContext": {"key": "u-k"}},
     {**NATIVE_OK, "value": None, "variationType": "SdkDefault", "reason": "DISABLED"}),
    ("missing", {"evaluationContext": {"key": "u-m"}, "defaultValue": 42},
     {**NATIVE_OK, "value": 42, "variationType": "SdkDefault", "reason": "ERROR", "errorCode": "FLAG_NOT_FOUND", "failed": True}),
    ("missing", {"evaluationContext": {"key": "u-m"}},
     {**NATIVE_OK, "value": None, "variationType": "SdkDefault", "reason": "ERROR", "errorCode": "FLAG_NOT_FOUND", "failed": True}),
    ("new-admin-access", {"evaluationContext": {"key": ""}, "defaultValue": 42},
     {**NATIVE_OK, "value": 42, "variationType": "SdkDefault", "reason": "ERROR", "errorCode": "TARGETING_KEY_MISSING",
      "errorDetails": "*", "failed": True}),
    ("notrack", {"evaluationContext": {"key": "u-n"}},
     {**NATIVE_OK, "value": "A", "variationType": "a", "reason": "STATIC", "trackEvents": False}),
])
def test_native_single_evaluation(module_relay, key, body, expected):
    assert_response(module_relay.feature(key, body), 200, expected, ignore=V1_UNGRADED)


@pytest.mark.parametrize("raw", [b'{"defaultValue": 1}', b'{"evaluationContext": null}', b"", b"{x"])
def test_native_endpoints_reject_requests_without_context(module_relay, raw):
    assert module_relay.request("POST", "/v1/feature/ver/eval", raw=raw)[0] == 400
    assert module_relay.request("POST", "/v1/allflags", raw=raw)[0] == 400


def all_entry(value, variation, reason, metadata=None, track=True, error=""):
    out = {"value": value, "variationType": variation, "reason": reason, "errorCode": error, "trackEvents": track}
    if metadata:
        out["metadata"] = metadata
    if error:
        out["errorDetails"] = "*"
    return out


def test_native_all_flags(module_relay):
    rule = {"evaluatedRuleName": "rule1"}
    assert_allflags(module_relay.allflags({"evaluationContext": {"key": KEY, "custom": {"anonymous": True}}}), True, {
        "array-flag": all_entry(["batmanFalse", "supermanFalse", "superherosFalse"], "False", "TARGETING_MATCH_SPLIT", rule),
        "disable-flag": all_entry(None, "", "DISABLED"),
        "flag-only-for-admin": all_entry(False, "Default", "DEFAULT"),
        "new-admin-access": all_entry(True, "True", "SPLIT"),
        "notrack": all_entry("A", "a", "STATIC", track=False),
        "number-flag": all_entry(2, "True", "TARGETING_MATCH", rule),
        "targeting-key-rule": all_entry(False, "false_var", "DEFAULT"),
        "test-flag-rule-apply": all_entry(OBJ, "Default", "DEFAULT"),
        "test-flag-rule-apply-false": all_entry({"test3": "test"}, "False", "TARGETING_MATCH_SPLIT", rule),
        "test-flag-rule-not-apply": all_entry(OBJ, "Default", "DEFAULT"),
        "ver": all_entry("A", "a", "STATIC", {"desc": "d", "issue": 12}),
    })
    missing = all_entry(None, "", "ERROR", error="TARGETING_KEY_MISSING")
    assert_allflags(module_relay.allflags({"evaluationContext": {"key": ""}}), False, {
        "array-flag": missing, "disable-flag": missing, "flag-only-for-admin": missing, "new-admin-access": missing,
        "notrack": all_entry("A", "a", "STATIC", track=False), "number-flag": missing,
        "targeting-key-rule": all_entry(False, "false_var", "DEFAULT"),
        "test-flag-rule-apply": missing, "test-flag-rule-apply-false": missing, "test-flag-rule-not-apply": missing,
        "ver": all_entry("A", "a", "STATIC", {"desc": "d", "issue": 12}),
    })


def test_empty_store(store, launch):
    relay = launch(store)
    assert_response(relay.bulk({"targetingKey": "u"}), 200, {"flags": []})
    assert_allflags(relay.allflags({"evaluationContext": {"key": "u"}}), True, None)
    assert_response(relay.ofrep("anything", {"targetingKey": "u"}), 404,
                    {"key": "anything", "errorCode": "FLAG_NOT_FOUND", "errorDetails": "*"})
