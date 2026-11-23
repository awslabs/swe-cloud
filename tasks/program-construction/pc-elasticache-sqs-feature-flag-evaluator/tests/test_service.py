"""Invocation, configuration, start-up and the flag-store key layout."""

import time

import pytest
import redis

from relaykit import Relay, Store, V1_UNGRADED, assert_bulk, assert_response, config_text, free_port

STATIC = {"variations": {"a": "A", "b": "B"}, "defaultRule": {"variation": "a"}}


def start_raw(scratch, text):
    port = free_port()
    return Relay(scratch, text.replace("PORT", str(port)), port)


def exit_status(relay, timeout=40):
    """Wait for the process to end on its own; record whether it ever served /health."""
    served = False
    deadline = time.time() + timeout
    while relay.proc.poll() is None and time.time() < deadline:
        try:
            served = served or relay.request("GET", "/health", timeout=1)[0] == 200
        except OSError:
            pass
        time.sleep(0.2)
    status = relay.proc.poll()
    relay.stop()
    return status, served


def test_health_and_sigterm(store, launch):
    store.put("f", STATIC)
    relay = launch(store)
    assert_response(relay.request("GET", "/health"), 200, {"initialized": True})
    relay.proc.terminate()
    assert relay.proc.wait(15) == 0, relay.log_tail()


@pytest.mark.parametrize("case", ["exporter without queueUrl", "retriever without redisOptions",
                                  "options/prefix spelling", "no retriever", "malformed yaml"])
def test_invalid_configuration_exits_nonzero(valkey, scratch, case):
    addr = f'"{valkey[0]}:{valkey[1]}"'
    text = {
        "exporter without queueUrl": f"server:\n  port: PORT\nretrievers:\n  - kind: redis\n    redisOptions:\n      addr: {addr}\n    redisPrefix: \"cfg:\"\nexporters:\n  - kind: sqs\n",
        "retriever without redisOptions": "server:\n  port: PORT\nretrievers:\n  - kind: redis\n    redisPrefix: \"cfg:\"\n",
        "options/prefix spelling": f"server:\n  port: PORT\nretrievers:\n  - kind: redis\n    options:\n      addr: {addr}\n    prefix: \"cfg:\"\n",
        "no retriever": "server:\n  port: PORT\nhideBanner: true\n",
        "malformed yaml": "server: [1\n",
    }[case]
    status, served = exit_status(start_raw(scratch, text))
    assert status is not None and status != 0 and not served, (status, served)


def test_unreachable_store_ends_the_process(scratch):
    text = "server:\n  port: PORT\nhideBanner: true\nretrievers:\n  - kind: redis\n    redisOptions:\n      addr: \"127.0.0.1:1\"\n    redisPrefix: \"cfg:\"\n"
    status, served = exit_status(start_raw(scratch, text), timeout=45)
    assert status == 0 and not served, (status, served)


def test_unreadable_store_at_startup_ends_the_process(store, scratch):
    store.put("good", STATIC)
    store.put("broken", "{not json")
    port = free_port()
    relay = Relay(scratch, config_text(port, store), port)
    status, served = exit_status(relay)
    assert status == 0 and not served, (status, served)


def test_prefix_selects_and_strips_keys(store, launch):
    store.put("alpha", {"variations": {"x": 1}, "defaultRule": {"variation": "x"}})
    store.put("team:beta", {"variations": {"x": 2}, "defaultRule": {"variation": "x"}})
    other = Store(store.endpoint, prefix="x" + store.prefix)  # contains the prefix, but not at the start
    sibling = Store(store.endpoint, prefix=store.prefix[:-1] + "-other:")
    try:
        other.put("gamma", STATIC)
        sibling.put("delta", STATIC)
        relay = launch(store)
        assert_bulk(relay.bulk({"targetingKey": "u"}), [
            {"key": "alpha", "value": 1, "variant": "x", "reason": "STATIC"},
            {"key": "team:beta", "value": 2, "variant": "x", "reason": "STATIC"},
        ])
        assert_response(relay.ofrep("team:beta", {"targetingKey": "u"}), 200, {
            "key": "team:beta", "value": 2, "variant": "x", "reason": "STATIC",
            "metadata": {"gofeatureflag_cacheable": True}})
        assert relay.ofrep(store.prefix + "alpha", {"targetingKey": "u"})[0] == 404
        assert relay.ofrep("gamma", {"targetingKey": "u"})[0] == 404
    finally:
        other.cleanup()
        sibling.cleanup()


def test_without_prefix_every_key_of_the_database_is_a_flag(valkey, launch):
    db = 5
    client = redis.Redis(host=valkey[0], port=valkey[1], db=db)
    client.flushdb()
    elsewhere = Store(valkey)  # database 0 must not be read
    try:
        client.set("plain", '{"variations": {"on": true}, "defaultRule": {"variation": "on"}}')
        client.set("ns:nested", '{"variations": {"n": 7}, "defaultRule": {"variation": "n"}}')
        elsewhere.put("db-zero", STATIC)
        relay = launch(Store(valkey, prefix=""), prefix=False, db=db)
        assert_bulk(relay.bulk({"targetingKey": "u"}), [
            {"key": "ns:nested", "value": 7, "variant": "n", "reason": "STATIC"},
            {"key": "plain", "value": True, "variant": "on", "reason": "STATIC"},
        ])
    finally:
        client.flushdb()
        elsewhere.cleanup()


def test_runs_without_exporter(store, launch):
    store.put("f", STATIC)
    relay = launch(store)
    assert_response(relay.ofrep("f", {"targetingKey": "u"}), 200, {
        "key": "f", "value": "A", "variant": "a", "reason": "STATIC", "metadata": {"gofeatureflag_cacheable": True}})
    assert_response(relay.feature("f", {"evaluationContext": {"key": "u"}}), 200, {
        "value": "A", "variationType": "a", "reason": "STATIC", "errorCode": "", "failed": False,
        "trackEvents": True, "version": ""}, ignore=V1_UNGRADED)
