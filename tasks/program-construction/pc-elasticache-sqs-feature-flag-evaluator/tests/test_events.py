"""Feature events delivered to SQS: which requests emit them and what each event contains.

Native observations: preparation/probe.sh log, experiments/scen_eval.py, scen_life2.py,
scen_more.py (curation.md). Event shape adapts upstream exporter/feature_event_test.go.
"""

import time

from relaykit import assert_events, drain, event_bodies, feature_event

FLAGS = {
    "banner": {"variations": {"on": True, "off": False},
               "targeting": [{"name": "staff", "query": "email ew \"@example.com\"", "variation": "on"}],
               "defaultRule": {"variation": "off"}, "version": "4.2"},
    "limits": {"variations": {"small": {"max": 10, "tags": ["a"]}, "large": {"max": 1000.5, "tags": []}},
               "defaultRule": {"variation": "large"}},
    # Same name and split as test_evaluation.py's pct60, whose native bucket for key-0 is "a".
    "pct60": {"variations": {"a": "A", "b": "B"}, "defaultRule": {"percentage": {"a": 30, "b": 30}}},
    "killed": {"variations": {"a": 1, "b": 2}, "defaultRule": {"variation": "a"}, "disable": True},
    "quiet": {"variations": {"a": "A"}, "defaultRule": {"variation": "a"}, "trackEvents": False},
    "broken": {"variations": {"a": "A", "b": 1}, "defaultRule": {"variation": "a"}},
}


def start(store, queue, launch):
    store.put_all(FLAGS)
    return launch(store, queue)


def test_single_flag_evaluations_emit_one_event_each(store, queue, launch):
    relay = start(store, queue, launch)
    begin = time.time()
    staff = {"targetingKey": "u-1", "email": "ann@example.com"}
    assert relay.ofrep("banner", staff)[0] == 200
    assert relay.ofrep("banner", staff)[0] == 200
    assert relay.ofrep("banner", {"targetingKey": "u-2", "email": "bob@else.org"})[0] == 200
    assert relay.feature("limits", {"evaluationContext": {"key": "u-3"}, "defaultValue": {}})[0] == 200
    assert relay.ofrep("limits", {"targetingKey": "u-4"})[0] == 200
    assert relay.ofrep("pct60", {"targetingKey": "key-0"})[0] == 200
    expected = [
        feature_event("banner", "u-1", "on", True, version="4.2"),
        feature_event("banner", "u-1", "on", True, version="4.2"),
        feature_event("banner", "u-2", "off", False, version="4.2"),
        feature_event("limits", "u-3", "large", {"max": 1000.5, "tags": []}),
        feature_event("limits", "u-4", "large", {"max": 1000.5, "tags": []}),
        feature_event("pct60", "key-0", "a", "A"),
    ]
    assert_events(event_bodies(drain(queue, len(expected)), begin, time.time()), expected)


def test_bulk_endpoints_and_rejected_requests_emit_nothing(store, queue, launch):
    relay = start(store, queue, launch)
    begin = time.time()
    assert relay.bulk({"targetingKey": "b-1"})[0] == 200
    assert relay.allflags({"evaluationContext": {"key": "b-2"}})[0] == 200
    assert relay.request("POST", "/ofrep/v1/evaluate/flags/banner", {})[0] == 400
    assert relay.request("POST", "/ofrep/v1/evaluate/flags/banner", raw=b"{bad")[0] == 400
    assert relay.request("POST", "/v1/feature/banner/eval", {"defaultValue": 1})[0] == 400
    assert relay.ofrep("quiet", {"targetingKey": "b-3"})[0] == 200
    assert relay.feature("quiet", {"evaluationContext": {"key": "b-4"}})[0] == 200
    assert relay.ofrep("banner", {"targetingKey": "sentinel"})[0] == 200
    assert_events(event_bodies(drain(queue, 1, quiet=4), begin, time.time()),
                  [feature_event("banner", "sentinel", "off", False, version="4.2")])


def test_failed_and_disabled_evaluations(store, queue, launch):
    relay = start(store, queue, launch)
    begin = time.time()
    relay.ofrep("missing", {"targetingKey": "e-1"})
    relay.feature("missing", {"evaluationContext": {"key": "e-2"}, "defaultValue": 42})
    relay.ofrep("pct60", {"plan": "x"})
    relay.feature("pct60", {"evaluationContext": {"key": ""}, "defaultValue": "GREY"})
    relay.ofrep("killed", {"targetingKey": "e-5"})
    relay.feature("killed", {"evaluationContext": {"key": "e-6"}, "defaultValue": 42})
    relay.ofrep("broken", {"targetingKey": "e-7"})
    relay.ofrep("limits", {})
    expected = [
        feature_event("missing", "e-1", "SdkDefault", None, default=True),
        feature_event("missing", "e-2", "SdkDefault", 42, default=True),
        feature_event("pct60", "", "SdkDefault", None, default=True),
        feature_event("pct60", "", "SdkDefault", "GREY", default=True),
        feature_event("killed", "e-5", "SdkDefault", None),
        feature_event("killed", "e-6", "SdkDefault", 42),
        feature_event("broken", "e-7", "SdkDefault", None, default=True),
        feature_event("limits", "", "large", {"max": 1000.5, "tags": []}),
    ]
    assert_events(event_bodies(drain(queue, len(expected)), begin, time.time()), expected)


def test_anonymous_contexts(store, queue, launch):
    relay = start(store, queue, launch)
    begin = time.time()
    relay.ofrep("banner", {"targetingKey": "a-1", "anonymous": True})
    relay.ofrep("banner", {"targetingKey": "a-2", "anonymous": "true"})
    relay.feature("banner", {"evaluationContext": {"key": "a-3", "custom": {"anonymous": True}}})
    relay.feature("banner", {"evaluationContext": {"key": "a-4", "anonymous": True}})
    expected = [
        feature_event("banner", "a-1", "off", False, version="4.2", kind="anonymousUser"),
        feature_event("banner", "a-2", "off", False, version="4.2"),
        feature_event("banner", "a-3", "off", False, version="4.2", kind="anonymousUser"),
        feature_event("banner", "a-4", "off", False, version="4.2"),
    ]
    assert_events(event_bodies(drain(queue, len(expected)), begin, time.time()), expected)
