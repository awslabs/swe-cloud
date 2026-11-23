"""Polling refresh: store changes after start-up, and refreshes that are rejected.

Native observations: experiments/scen_life2.py ("after ..." lines) and scen_more.py ("mixed ..."
lines); see curation.md. Upstream retriever/redisretriever/retriever_test.go stores one JSON flag per key.
"""

import time

import pytest

from relaykit import V1_UNGRADED, assert_bulk, assert_events, assert_response, drain, event_bodies, feature_event, wait_for

TWO = {"variations": {"a": "A", "b": "B"}, "defaultRule": {"variation": "a"}}
TWO_B = {"variations": {"a": "A", "b": "B"}, "defaultRule": {"variation": "b"}}
POLLS = 2.6  # more than two polling intervals of 1000 ms


def static(key, value, variant):
    return {"key": key, "value": value, "variant": variant, "reason": "STATIC"}


def bulk_keys(relay):
    status, body = relay.bulk({"targetingKey": "poll"})
    return [f.get("key") for f in body["flags"]] if status == 200 and isinstance(body, dict) else None


def test_added_changed_and_deleted_keys(store, queue, launch):
    store.put("f1", {**TWO, "version": "1"})
    store.put("f2", {"variations": {"x": 1}, "defaultRule": {"variation": "x"}})
    relay = launch(store, queue)
    begin = time.time()
    assert_response(relay.ofrep("f1", {"targetingKey": "before"}), 200,
                    {**static("f1", "A", "a"), "metadata": {"gofeatureflag_cacheable": True}})

    store.put("f1", {**TWO_B, "version": "2"})
    store.put("f3", {"variations": {"on": True}, "defaultRule": {"variation": "on"}})
    store.delete("f2")
    final = [static("f1", "B", "b"), static("f3", True, "on")]
    wait_for(lambda: relay.bulk({"targetingKey": "poll"}),
             lambda r: r[0] == 200 and isinstance(r[1], dict) and [f.get("variant") for f in r[1].get("flags", [])] == ["b", "on"])
    assert_bulk(relay.bulk({"targetingKey": "u"}), final)
    assert_response(relay.ofrep("f2", {"targetingKey": "after"}), 404, {"key": "f2", "errorCode": "FLAG_NOT_FOUND", "errorDetails": "*"})
    assert_response(relay.feature("f1", {"evaluationContext": {"key": "after"}}), 200, {
        "value": "B", "variationType": "b", "reason": "STATIC", "errorCode": "", "failed": False,
        "trackEvents": True, "version": "2"}, ignore=V1_UNGRADED)
    assert_events(event_bodies(drain(queue, 3), begin, time.time()), [
        feature_event("f1", "before", "a", "A", version="1"),
        feature_event("f2", "after", "SdkDefault", None, default=True),
        feature_event("f1", "after", "b", "B", version="2"),
    ])


def test_unparsable_key_rejects_every_refresh_until_fixed(store, launch):
    store.put("f1", TWO)
    store.put("f3", {"variations": {"x": 1}, "defaultRule": {"variation": "x"}})
    relay = launch(store)
    store.put("f3", "{broken")
    time.sleep(POLLS)
    store.put("f1", TWO_B)
    store.put("f4", {"variations": {"y": "Y"}, "defaultRule": {"variation": "y"}})
    store.delete("missing-anyway")
    time.sleep(POLLS)
    # The previous flag set, including f3's last good definition, is still served.
    assert_bulk(relay.bulk({"targetingKey": "u"}), [static("f1", "A", "a"), static("f3", 1, "x")])
    assert_response(relay.request("GET", "/health"), 200, {"initialized": True})
    assert relay.ofrep("f4", {"targetingKey": "u"})[0] == 404

    store.put("f3", {"variations": {"x": 2}, "defaultRule": {"variation": "x"}})
    wait_for(lambda: bulk_keys(relay), lambda keys: keys == ["f1", "f3", "f4"])
    assert_bulk(relay.bulk({"targetingKey": "u"}), [static("f1", "B", "b"), static("f3", 2, "x"), static("f4", "Y", "y")])


def test_invalid_definition_at_refresh_removes_only_that_flag(store, launch):
    store.put("f1", TWO)
    store.put("f3", {"variations": {"x": 1}, "defaultRule": {"variation": "x"}})
    relay = launch(store)
    store.put("f3", {"variations": {"x": 1, "y": "s"}, "defaultRule": {"variation": "x"}})
    store.put("f1", TWO_B)
    wait_for(lambda: relay.bulk({"targetingKey": "poll"}),
             lambda r: r[0] == 200 and isinstance(r[1], dict) and [f.get("variant") for f in r[1].get("flags", [])] == ["b"])
    assert_bulk(relay.bulk({"targetingKey": "u"}), [static("f1", "B", "b")])
    assert relay.ofrep("f3", {"targetingKey": "u"})[0] == 404


@pytest.mark.parametrize(("odd", "blocks"), [
    ("hash", True), ("5", True), ("[1]", True), ('"text"', True), ("null", False), ("{}", False),
])
def test_values_that_are_not_flag_objects(store, launch, odd, blocks):
    store.put("f1", TWO)
    relay = launch(store)
    if odd == "hash":
        store.client.hset(store.prefix + "odd", "a", "b")
    else:
        store.put("odd", odd)
    time.sleep(POLLS)
    store.put("f1", TWO_B)
    time.sleep(POLLS)
    expected = static("f1", "A", "a") if blocks else static("f1", "B", "b")
    assert_bulk(relay.bulk({"targetingKey": "u"}), [expected])
