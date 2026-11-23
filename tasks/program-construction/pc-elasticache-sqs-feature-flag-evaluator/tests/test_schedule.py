"""Time-dependent flags: scheduled steps, experimentation windows and progressive rollouts.

The evaluation instant is pinned with the context attribute gofeatureflag.currentDateTime.
Expectations are native observations (experiments/scen_query.py, scen_more.py; curation.md).
Progressive-rollout shapes adapt upstream modules/core/flag/rule_progressive_rollout_test.go.
"""

import pytest

from relaykit import V1_UNGRADED, assert_response

FLAGS = {
    "sched": {"variations": {"a": "A", "b": "B", "c": "C", "d": "D"},
              "targeting": [{"name": "r", "query": "beta eq true", "variation": "a"}],
              "defaultRule": {"name": "dflt", "variation": "b"},
              "scheduledRollout": [
                  {"date": "2024-06-01T00:00:00Z", "defaultRule": {"variation": "a"}, "version": "2"},
                  {"date": "2024-01-01T00:00:00Z", "targeting": [{"name": "r", "variation": "c"}]},
                  {"date": "2025-01-01T00:00:00Z", "disable": True},
                  {"date": "2024-03-01T00:00:00Z", "targeting": [{"name": "new", "query": "plan eq \"pro\"", "variation": "d"}],
                   "variations": {"d": "DD"}},
                  {"targeting": [{"name": "r", "variation": "d"}]},
                  {"date": "2024-08-01T00:00:00Z", "trackEvents": False, "targeting": [{"name": "r", "disable": True}]},
              ]},
    "expe": {"variations": {"on": True, "off": False}, "defaultRule": {"variation": "on"},
             "experimentation": {"start": "2024-01-01T00:00:00Z", "end": "2024-02-01T00:00:00Z"}},
    "expe-start": {"variations": {"on": 1, "off": 0}, "defaultRule": {"variation": "on"},
                   "experimentation": {"start": "2024-01-01T00:00:00Z"}},
    "prog": {"variations": {"old": "OLD", "new": "NEW"},
             "defaultRule": {"progressiveRollout": {
                 "initial": {"variation": "old", "date": "2024-01-01T00:00:00Z", "percentage": 0},
                 "end": {"variation": "new", "date": "2024-01-11T00:00:00Z", "percentage": 100}}}},
    "prog2": {"variations": {"old": "OLD", "new": "NEW"},
              "targeting": [{"name": "pr", "query": "beta eq true", "progressiveRollout": {
                  "initial": {"variation": "old", "date": "2024-01-01T00:00:00Z", "percentage": 20},
                  "end": {"variation": "new", "date": "2024-01-11T00:00:00Z", "percentage": 60}}}],
              "defaultRule": {"variation": "old"}},
    "both-prog": {"variations": {"a": "A", "b": "B", "c": "C"},
                  "defaultRule": {"variation": "c", "percentage": {"c": 100}, "progressiveRollout": {
                      "initial": {"variation": "a", "date": "2024-01-01T00:00:00Z"},
                      "end": {"variation": "b", "date": "2024-01-02T00:00:00Z"}}}},
    "sched-pct": {"variations": {"a": "A", "b": "B"}, "defaultRule": {"name": "d", "variation": "a"},
                  "scheduledRollout": [{"date": "2024-01-01T00:00:00Z", "defaultRule": {"percentage": {"a": 50, "b": 50}}}]},
}

U1 = {"targetingKey": "u1", "beta": True, "plan": "pro"}
U2 = {"targetingKey": "u2", "beta": False, "plan": "free"}


def at(context, when):
    return {**context, "gofeatureflag": {"currentDateTime": when}} if when else dict(context)


def result(key, value, variant, reason, rule=None):
    body = {"key": key, "value": value, "variant": variant, "reason": reason}
    if rule:
        body["metadata"] = {"evaluatedRuleName": rule}
    return 200, body


DISABLED = (None, "SdkDefault", "DISABLED")

SCHEDULE = [
    # when, (u1 value, variant, reason, rule), (u2 ...)
    ("2023-12-31T23:59:59Z", ("A", "a", "TARGETING_MATCH", "r"), ("B", "b", "DEFAULT")),
    ("2024-01-01T00:00:00Z", ("C", "c", "TARGETING_MATCH", "r"), ("B", "b", "DEFAULT")),
    ("2024-03-15T00:00:00Z", ("C", "c", "TARGETING_MATCH", "r"), ("B", "b", "DEFAULT")),
    ("2024-07-01T00:00:00Z", ("C", "c", "TARGETING_MATCH", "r"), ("A", "a", "DEFAULT")),
    ("2024-09-01T00:00:00Z", ("DD", "d", "TARGETING_MATCH", "new"), ("A", "a", "DEFAULT")),
    ("2025-02-01T00:00:00Z", DISABLED, DISABLED),
    (None, DISABLED, DISABLED),
    ("not-a-date", DISABLED, DISABLED),
]


@pytest.mark.parametrize(("when", "first", "second"), SCHEDULE, ids=[str(w) for w, _, _ in SCHEDULE])
def test_scheduled_steps_apply_by_date(module_relay, when, first, second):
    for context, expected in ((U1, first), (U2, second)):
        assert_response(module_relay.ofrep("sched", at(context, when)), *result("sched", *expected), label=context["targetingKey"])


def test_scheduled_steps_on_native_endpoint(module_relay):
    base = {"errorCode": "", "failed": False, "trackEvents": True, "version": ""}
    cases = [
        ("2024-07-01T00:00:00Z", U2, {"value": "A", "variationType": "a", "reason": "DEFAULT"}),
        ("2024-09-01T00:00:00Z", U1, {"value": "DD", "variationType": "d", "reason": "TARGETING_MATCH",
                                      "metadata": {"evaluatedRuleName": "new"}}),
        ("2025-02-01T00:00:00Z", U1, {"value": "Z", "variationType": "SdkDefault", "reason": "DISABLED"}),
    ]
    for when, context, expected in cases:
        custom = {k: v for k, v in at(context, when).items() if k != "targetingKey"}
        body = {"evaluationContext": {"key": context["targetingKey"], "custom": custom}, "defaultValue": "Z"}
        assert_response(module_relay.feature("sched", body), 200, {**base, **expected}, ignore=V1_UNGRADED, label=when)


@pytest.mark.parametrize(("key", "when", "expected"), [
    ("expe", "2023-12-31T23:59:59Z", DISABLED),
    ("expe", "2024-01-01T00:00:00Z", (True, "on", "STATIC")),
    ("expe", "2024-01-15T00:00:00Z", (True, "on", "STATIC")),
    ("expe", "2024-02-01T00:00:00Z", (True, "on", "STATIC")),
    ("expe", "2024-02-01T00:00:01Z", DISABLED),
    ("expe", None, DISABLED),
    ("expe-start", "2023-01-01T00:00:00Z", DISABLED),
    ("expe-start", "2030-01-01T00:00:00Z", (1, "on", "STATIC")),
    ("expe-start", None, (1, "on", "STATIC")),
])
def test_experimentation_window(module_relay, key, when, expected):
    assert_response(module_relay.ofrep(key, at(U2, when)), *result(key, *expected))


# Native variants for targeting keys key-0 .. key-15 (experiments/scen_query.py "P" lines).
PROGRESSIVE = {
    "prog": [
        ("2023-06-01T00:00:00Z", "oooooooooooooooo"),
        ("2024-01-03T00:00:00Z", "ooooooonnooonooo"),
        ("2024-01-06T00:00:00Z", "nnoonoonnnonnoon"),
        ("2024-01-09T00:00:00Z", "nnnonnonnnnnnonn"),
        ("2024-01-11T00:00:00Z", "nnnnnnnnnnnnnnnn"),
        (None, "nnnnnnnnnnnnnnnn"),
    ],
    "prog2": [
        ("2023-06-01T00:00:00Z", "oooooooooooooooo"),
        ("2024-01-03T00:00:00Z", "onoonooooooonooo"),
        ("2024-01-06T00:00:00Z", "onoonnoooooonoon"),
        ("2024-01-09T00:00:00Z", "onnonnnonoonnoon"),
        ("2024-01-11T00:00:00Z", "onnonnnonoonnonn"),
        (None, "onnonnnonoonnonn"),
    ],
}


@pytest.mark.parametrize("key", sorted(PROGRESSIVE))
def test_progressive_rollout(module_relay, key):
    reason, rule = ("SPLIT", None) if key == "prog" else ("TARGETING_MATCH_SPLIT", "pr")
    for when, row in PROGRESSIVE[key]:
        for i, letter in enumerate(row):
            variant = {"o": "old", "n": "new"}[letter]
            assert_response(module_relay.ofrep(key, at({"targetingKey": f"key-{i}", "beta": True}, when)),
                            *result(key, variant.upper(), variant, reason, rule), label=f"{when} key-{i}")


def test_progressive_rollout_takes_precedence_over_percentage(module_relay):
    assert_response(module_relay.ofrep("both-prog", at({"targetingKey": "u1"}, "2024-06-01T00:00:00Z")),
                    *result("both-prog", "B", "b", "SPLIT"))


def test_scheduled_split_requires_targeting_key_at_any_date(module_relay):
    assert_response(module_relay.ofrep("sched-pct", {"targetingKey": "u1"}), *result("sched-pct", "A", "a", "SPLIT"))
    assert_response(module_relay.ofrep("sched-pct", at({"targetingKey": "u1"}, "2023-01-01T00:00:00Z")),
                    *result("sched-pct", "A", "a", "STATIC"))
    for when in (None, "2023-01-01T00:00:00Z"):
        assert_response(module_relay.ofrep("sched-pct", at({"plan": "x"}, when)), 400,
                        {"key": "sched-pct", "errorCode": "TARGETING_KEY_MISSING", "errorDetails": "*"}, label=str(when))
