"""Chunked HTTP publish/subscribe streams (adapted from upstream TestPubSub in tests/basic.py)."""

import pytest

from gw import Stream, assert_json, assert_jsonp, get, loads, same, same_msgpack, unpack, wait_for


def subscribe(g, path, headers=()):
    s = Stream(g.port, path, headers=headers)
    assert s.read_head() == 200
    return s


def publish(g, channel, message):
    return loads(get(g.port, f"/PUBLISH/{channel}/{message}").body)


@pytest.mark.timeout(600)
def test_subscribe_streams_json_messages_in_order(gateway, key):
    g = gateway()
    ch = f"{key}:ch"
    s = subscribe(g, f"/SUBSCRIBE/{ch}")
    try:
        assert s.header("Content-Type") == "application/json"
        assert s.header("Transfer-Encoding") == "chunked"
        assert s.header("Content-Length") is None
        same(loads(s.next_chunk()), {"SUBSCRIBE": ["subscribe", ch, 1]})
        messages = [f"m{i}" for i in range(20)] + ["a%2fb%20c"]
        for m in messages:
            same(publish(g, ch, m), {"PUBLISH": 1})
        for m in messages[:-1]:
            same(loads(s.next_chunk()), {"SUBSCRIBE": ["message", ch, m]})
        same(loads(s.next_chunk()), {"SUBSCRIBE": ["message", ch, "a/b c"]})
        # other channels are not delivered
        same(publish(g, f"{key}:other", "x"), {"PUBLISH": 0})
        same(publish(g, ch, "last"), {"PUBLISH": 1})
        same(loads(s.next_chunk()), {"SUBSCRIBE": ["message", ch, "last"]})
    finally:
        s.close()


def test_subscribe_with_jsonp(gateway, key):
    g = gateway()
    ch = f"{key}:jp"
    s = subscribe(g, f"/SUBSCRIBE/{ch}?jsonp=onEvent")
    try:
        assert_jsonp(s.next_chunk(), "onEvent", {"SUBSCRIBE": ["subscribe", ch, 1]})
        publish(g, ch, "hello")
        assert_jsonp(s.next_chunk(), "onEvent", {"SUBSCRIBE": ["message", ch, "hello"]})
    finally:
        s.close()


def test_subscribe_raw_and_msgpack_formats(gateway, key):
    g = gateway()
    ch = f"{key}:fmt"
    raw = subscribe(g, f"/SUBSCRIBE/{ch}.raw")
    msg = subscribe(g, f"/SUBSCRIBE/{ch}.msg")
    try:
        assert raw.header("Content-Type") == "binary/octet-stream"
        assert msg.header("Content-Type") == "application/x-msgpack"
        c = ch.encode()
        assert raw.next_chunk() == b"*3\r\n$9\r\nsubscribe\r\n$%d\r\n%s\r\n$1\r\n1\r\n" % (len(c), c)
        same_msgpack(unpack(msg.next_chunk()), {"SUBSCRIBE": ["subscribe", ch, 1]})
        same(publish(g, ch, "x1"), {"PUBLISH": 2})
        assert raw.next_chunk() == b"*3\r\n$7\r\nmessage\r\n$%d\r\n%s\r\n$2\r\nx1\r\n" % (len(c), c)
        same_msgpack(unpack(msg.next_chunk()), {"SUBSCRIBE": ["message", ch, "x1"]})
    finally:
        raw.close()
        msg.close()


def test_psubscribe_pattern_messages(gateway, key):
    g = gateway()
    s = subscribe(g, f"/PSUBSCRIBE/{key}:p*")
    try:
        same(loads(s.next_chunk()), {"PSUBSCRIBE": ["psubscribe", f"{key}:p*", 1]})
        same(publish(g, f"{key}:px", "one"), {"PUBLISH": 1})
        same(publish(g, f"{key}:q", "skip"), {"PUBLISH": 0})
        same(publish(g, f"{key}:p2", "two"), {"PUBLISH": 1})
        same(loads(s.next_chunk()), {"PSUBSCRIBE": ["pmessage", f"{key}:p*", f"{key}:px", "one"]})
        same(loads(s.next_chunk()), {"PSUBSCRIBE": ["pmessage", f"{key}:p*", f"{key}:p2", "two"]})
    finally:
        s.close()


def test_subscribe_keyed_by_command_spelling_and_db_prefix(gateway, key):
    g = gateway()
    ch = f"{key}:lc"
    lower = subscribe(g, f"/subscribe/{ch}")
    prefixed = subscribe(g, f"/3/SUBSCRIBE/{ch}")
    try:
        same(loads(lower.next_chunk()), {"subscribe": ["subscribe", ch, 1]})
        same(loads(prefixed.next_chunk()), {"SUBSCRIBE": ["subscribe", ch, 1]})
        same(publish(g, ch, "both"), {"PUBLISH": 2})
        same(loads(lower.next_chunk()), {"subscribe": ["message", ch, "both"]})
        same(loads(prefixed.next_chunk()), {"SUBSCRIBE": ["message", ch, "both"]})
    finally:
        lower.close()
        prefixed.close()


def test_closing_the_stream_releases_the_subscription(gateway, key):
    g = gateway()
    ch = f"{key}:bye"
    s = subscribe(g, f"/SUBSCRIBE/{ch}")
    s.next_chunk()
    same(publish(g, ch, "x"), {"PUBLISH": 1})
    s.close()
    assert wait_for(lambda: publish(g, ch, "y") == {"PUBLISH": 0}, timeout=10)
    # the gateway keeps serving normal requests
    assert_json(get(g.port, "/PING"), {"PING": [True, "PONG"]})


def test_acl_denied_subscribe(gateway, key):
    g = gateway(acl=[{"disabled": ["SUBSCRIBE"]}])
    r = get(g.port, f"/SUBSCRIBE/{key}:no")
    assert r.status == 403
    same(publish(g, f"{key}:no", "x"), {"PUBLISH": 0})
