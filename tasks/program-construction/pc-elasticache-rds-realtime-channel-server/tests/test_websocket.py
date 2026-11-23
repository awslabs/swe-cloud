"""WebSocket connections, subscriptions, presence and client events (one server for the module)."""

import json
import re
import time

import pytest
from realtime import App, channel_auth, decoded, same, same_multiset
from websockets.exceptions import InvalidStatus


def error(code, message):
    return {"event": "pusher:error", "data": {"code": code, "message": message}}


def frames(client, wait=0.8):
    out = client.pending(wait)
    client.consume_all()
    return out


def post(server, app, body):
    r = server.api(app, "POST", f"/apps/{app.id}/events", body)
    assert r.status == 200, r


def presence_data(frame):
    """Decoded presence payload with `ids` order made irrelevant."""
    data = decoded(frame)["data"]
    assert set(data) == {"presence"} and set(data["presence"]) == {"ids", "hash", "count"}, data
    p = data["presence"]
    assert len(p["ids"]) == len(set(p["ids"])), p
    return {**p, "ids": sorted(p["ids"])}


# ── connections ─────────────────────────────────────────────────────────────────


def test_connection_established(shared):
    s, app = shared.server, shared.app("hello")
    seen = set()
    for query in ("protocol=7&client=js&version=8.4.0", "protocol=7", ""):
        c = s.ws(app.key, query)
        frame = c.handshake()
        assert set(frame) == {"event", "data"} and frame["event"] == "pusher:connection_established", frame
        data = decoded(frame)["data"]
        assert set(data) == {"socket_id", "activity_timeout"}, data
        assert same(data["activity_timeout"], 120), data
        assert isinstance(data["socket_id"], str) and re.fullmatch(r"\d+\.\d+", data["socket_id"]), data
        seen.add(data["socket_id"])
        c.close()
    assert len(seen) == 3
    with pytest.raises(InvalidStatus) as rejected:
        s.ws(app.key, "protocol=abc")
    assert rejected.value.response.status_code == 400


def test_unknown_and_disabled_apps_are_refused(shared):
    s = shared.server
    disabled = shared.app("off", enabled=False)
    for key in ("no-such-key", disabled.key):
        c = s.ws(key)
        assert c.handshake(5) is None
        assert c.wait_closed(5) is not None
        assert c.frames == []


def test_connection_limit(shared):
    s, app = shared.server, shared.app("max", max_connections=2)
    first, second = s.connect(app), s.connect(app)
    third = s.ws(app.key)
    assert third.handshake(5) is None and third.frames == []
    assert third.wait_closed(5) is not None
    # Existing connections are unaffected; a slot frees up once one leaves.
    first.send({"event": "pusher:ping"})
    assert frames(first) == [{"event": "pusher:pong"}]
    second.close()
    time.sleep(1)
    again = s.connect(app)
    again.close()
    first.close()


def test_ping_and_malformed_messages(shared):
    s, app = shared.server, shared.app("ping")
    c = s.connect(app)
    c.send({"event": "pusher:ping", "data": {}})
    assert frames(c) == [{"event": "pusher:pong"}]
    c.send({"event": "pusher:ping"})
    assert frames(c) == [{"event": "pusher:pong"}]

    c.send("not json at all")
    got = frames(c)
    assert len(got) == 1 and got[0]["event"] == "pusher:error" and set(got[0]["data"]) == {"code", "message"}, got
    assert same(got[0]["data"]["code"], 4000) and got[0]["data"]["message"].startswith("Invalid message format: "), got
    c.send({"data": {}})
    assert frames(c) == [error(4000, "Invalid event name: Event name is required")]
    c.send({"event": "pusher:no-such-thing", "data": {}})
    c.send({"event": "something-else", "data": {}})
    assert frames(c) == []
    # Errors do not close the connection.
    c.send({"event": "pusher:ping"})
    assert frames(c) == [{"event": "pusher:pong"}] and c.closed is None
    c.close()


# ── subscriptions ───────────────────────────────────────────────────────────────


def test_public_subscriptions_and_channel_names(shared):
    s, app = shared.server, shared.app("names")
    c = s.connect(app)
    for channel in ("lobby", "Ok_=@,;.x-1", "c" * 200, "lobby"):  # a repeated subscribe is acknowledged again
        c.subscribe(channel)
        got = frames(c)
        assert len(got) == 1 and same(decoded(got[0]), {"event": "pusher_internal:subscription_succeeded", "channel": channel, "data": {}}), got

    cases = [
        ("bad chan", error(4300, "Channel error: Channel name contains invalid characters: 'bad chan' (invalid chars: [' '])")),
        ("c" * 201, error(4300, "Channel error: Channel name too long. Max length is 200")),
        ("ns:x", error(4300, "Channel error: Unknown channel namespace 'ns'")),
        ("a#b", error(4300, "Channel error: Channel 'a#b' uses user-limited syntax but the namespace does not allow it")),
        ("wild*", error(4300, "Channel error: Wildcard subscriptions are only supported on protocol V2")),
    ]
    for channel, expected in cases:
        c.subscribe(channel)
        assert frames(c) == [expected], channel
    c.send({"event": "pusher:subscribe", "data": {}})
    assert frames(c) == [error(4000, "Invalid message format: Missing channel field")]
    c.send({"event": "pusher:subscribe"})
    assert frames(c) == [error(4000, "Invalid message format: Invalid subscription data format")]

    r = s.api(app, "GET", f"/apps/{app.id}/channels")
    assert same(r.json(), {"channels": {"lobby": {}, "Ok_=@,;.x-1": {}, "c" * 200: {}}}), r
    limited = shared.app("short", max_channel_name_length=20)
    d = s.connect(limited)
    d.subscribe("c" * 21)
    assert frames(d) == [error(4300, "Channel error: Channel name too long. Max length is 20")]
    d.join("c" * 20)
    c.close()
    d.close()


def test_private_channel_authentication(shared):
    s, app = shared.server, shared.app("priv")
    c = s.connect(app)
    required = error(4009, "Authentication error: Authentication signature required for this channel")
    rejected = error(4009, "Authentication error: Channel requires authentication")
    c.subscribe("private-x")
    assert frames(c) == [required]
    c.subscribe("private-encrypted-x")
    assert frames(c) == [required]
    for auth in ("garbage", channel_auth(app, "1.1", "private-x"), channel_auth(App(app.id, "other", app.secret), c.socket_id, "private-x"),
                 channel_auth(app, c.socket_id, "private-x").split(":", 1)[1], channel_auth(app, c.socket_id, "private-y")):
        c.subscribe("private-x", auth)
        assert frames(c) == [rejected], auth
    r = s.api(app, "GET", f"/apps/{app.id}/channels")
    assert same(r.json(), {"channels": {}}), r
    c.subscribe("private-x", channel_auth(app, c.socket_id, "private-x"))
    got = frames(c)
    assert len(got) == 1 and same(decoded(got[0]), {"event": "pusher_internal:subscription_succeeded", "channel": "private-x", "data": {}}), got
    c.close()


def test_presence_subscription_and_member_events(shared):
    s, app = shared.server, shared.app("pres")
    a, b = s.connect(app), s.connect(app)
    first = a.join("presence-room", app, "u1", {"name": "Ann", "tags": [1, 2]})
    assert set(first) == {"event", "channel", "data"}
    assert same(presence_data(first), {"ids": ["u1"], "hash": {"u1": {"name": "Ann", "tags": [1, 2]}}, "count": 1})
    second = b.join("presence-room", app, "u2", "plain")
    assert same(presence_data(second), {"ids": ["u1", "u2"], "hash": {"u1": {"name": "Ann", "tags": [1, 2]}, "u2": "plain"}, "count": 2})
    got = frames(a)
    assert len(got) == 1 and same(decoded(got[0]), {"event": "pusher_internal:member_added", "channel": "presence-room",
                                                    "data": {"user_id": "u2", "user_info": "plain"}}), got
    assert frames(b) == []

    # A member without user_info is listed with an empty object.
    c = s.connect(app)
    third = c.join("presence-room", app, "u3")
    assert same(presence_data(third)["hash"]["u3"], {})
    got = [decoded(f) for f in frames(a)]
    assert same(got, [{"event": "pusher_internal:member_added", "channel": "presence-room", "data": {"user_id": "u3", "user_info": {}}}]), got
    frames(b)

    # Leaving: unsubscribe and disconnect both announce member_removed to the others.
    c.send({"event": "pusher:unsubscribe", "data": {"channel": "presence-room"}})
    assert frames(c) == []
    removed = {"event": "pusher_internal:member_removed", "channel": "presence-room", "data": {"user_id": "u3"}}
    for client in (a, b):
        got = [decoded(f) for f in frames(client)]
        assert same(got, [removed]), got
    b.close()
    got = [decoded(f) for f in frames(a, 2.0)]
    assert same(got, [{**removed, "data": {"user_id": "u2"}}]), got
    a.close()
    c.close()


def test_presence_member_with_several_connections(shared):
    s, app = shared.server, shared.app("multi")
    a, b, c = s.connect(app), s.connect(app), s.connect(app)
    a.join("presence-m", app, "u1", {"v": 1})
    b.join("presence-m", app, "u2")
    frames(a)
    again = c.join("presence-m", app, "u1", {"v": 2})
    assert same(presence_data(again), {"ids": ["u1", "u2"], "hash": {"u1": {"v": 1}, "u2": {}}, "count": 2})
    assert frames(a) == [] and frames(b) == []
    r = s.api(app, "GET", f"/apps/{app.id}/channels/presence-m", params={"info": "user_count,subscription_count"})
    assert same(r.json(), {"occupied": True, "user_count": 2, "subscription_count": 3}), r

    # Subscribing the same socket again sends nothing at all.
    a.subscribe("presence-m", channel_auth(app, a.socket_id, "presence-m", json.dumps({"user_id": "u1"})), json.dumps({"user_id": "u1"}))
    assert frames(a) == []

    # u1 stays while one of its connections remains.
    c.send({"event": "pusher:unsubscribe", "data": {"channel": "presence-m"}})
    assert frames(b) == []
    a.close()
    got = [decoded(f) for f in frames(b, 2.0)]
    assert same(got, [{"event": "pusher_internal:member_removed", "channel": "presence-m", "data": {"user_id": "u1"}}]), got
    b.close()
    c.close()


def test_presence_channel_data_errors(shared):
    s, app = shared.server, shared.app("pdata")
    c = s.connect(app)
    member = json.dumps({"user_id": "u1", "user_info": {}})
    c.subscribe("presence-e", channel_data=member)
    assert frames(c) == [error(4009, "Authentication error: Authentication signature required for this channel")]
    c.subscribe("presence-e", channel_auth(app, c.socket_id, "presence-e"), member)
    assert frames(c) == [error(4009, "Authentication error: Channel requires authentication")]
    c.subscribe("presence-e", channel_auth(app, c.socket_id, "presence-e", member), "not json")
    assert frames(c) == [error(4000, "Invalid message format: Invalid channel_data JSON for presence")]
    for data in ({"user_id": 5}, {"user_info": {}}):
        text = json.dumps(data)
        c.subscribe("presence-e", channel_auth(app, c.socket_id, "presence-e", text), text)
        assert frames(c) == [error(4300, "Channel error: Missing user_id in channel_data")], data
    r = s.api(app, "GET", f"/apps/{app.id}/channels/presence-e/users")
    assert same(r.json(), {"users": []}), r
    c.close()


def test_presence_limits(shared):
    s, app = shared.server, shared.app("plim", max_presence_members_per_channel=2, max_presence_member_size_in_kb=1)
    a, b, c = s.connect(app), s.connect(app), s.connect(app)
    a.join("presence-l", app, "m0")
    b.join("presence-l", app, "m1")
    frames(a)
    text = json.dumps({"user_id": "m2", "user_info": {}})
    c.subscribe("presence-l", channel_auth(app, c.socket_id, "presence-l", text), text)
    assert frames(c) == [error(4100, "Over capacity")]
    assert frames(a) == [] and frames(b) == []
    r = s.api(app, "GET", f"/apps/{app.id}/channels/presence-l/users")
    assert same_multiset(r.json()["users"], [{"id": "m0"}, {"id": "m1"}]), r

    # Member data is limited in whole kilobytes.
    c.join("presence-size", app, "fits", "y" * 2047)
    text = json.dumps({"user_id": "big", "user_info": "y" * 2048})
    c.subscribe("presence-size", channel_auth(app, c.socket_id, "presence-size", text), text)
    assert frames(c) == [error(4300, "Channel error: Presence member data size (2KB) exceeds limit (1KB)")]
    for client in (a, b, c):
        client.close()


def test_unsubscribe(shared):
    s, app = shared.server, shared.app("unsub")
    a, b = s.connect(app), s.connect(app)
    a.join("room-u")
    b.join("room-u")
    a.send({"event": "pusher:unsubscribe", "data": {"channel": "room-u"}})
    assert frames(a) == []
    post(s, app, {"name": "after", "channel": "room-u", "data": "x"})
    assert frames(a) == []
    assert frames(b) == [{"event": "after", "channel": "room-u", "data": "x"}]
    a.send({"event": "pusher:unsubscribe", "data": {"channel": "never-joined"}})
    assert frames(a) == []
    a.send({"event": "pusher:unsubscribe"})
    assert frames(a) == [error(4000, "Invalid message format: Missing data in unsubscribe message")]
    r = s.api(app, "GET", f"/apps/{app.id}/channels/room-u", params={"info": "subscription_count"})
    assert same(r.json(), {"occupied": True, "subscription_count": 1}), r
    a.close()
    b.close()


# ── client events ───────────────────────────────────────────────────────────────


def test_client_events_are_relayed_to_other_subscribers(shared):
    s, app = shared.server, shared.app("client")
    a, b, c = s.connect(app), s.connect(app), s.connect(app)
    for client in (a, b):
        client.join("private-chat", app)
    c.join("presence-chat", app, "u3")
    a.join("presence-chat", app, "u1")
    frames(c)

    a.send({"event": "client-typing", "channel": "private-chat", "data": {"t": [1, 2.5, True, None]}})
    a.send({"event": "client-text", "channel": "private-chat", "data": "words"})
    got = frames(b)
    assert same(got, [{"event": "client-typing", "channel": "private-chat", "data": {"t": [1, 2.5, True, None]}},
                      {"event": "client-text", "channel": "private-chat", "data": "words"}]), got
    a.send({"event": "client-here", "channel": "presence-chat", "data": {"p": 1}})
    assert same(frames(c), [{"event": "client-here", "channel": "presence-chat", "data": {"p": 1}}])
    assert frames(a) == [] and frames(b) == []
    # A large payload is fine without an app limit.
    a.send({"event": "client-big", "channel": "private-chat", "data": "x" * 20000})
    assert frames(b) == [{"event": "client-big", "channel": "private-chat", "data": "x" * 20000}]
    for client in (a, b, c):
        client.close()


def test_client_event_rules(shared):
    s, app = shared.server, shared.app("rules")
    a, b = s.connect(app), s.connect(app)
    a.join("open")
    a.join("private-r", app)
    b.join("private-r", app)
    cases = [
        ({"event": "client-x", "channel": "open", "data": {}},
         error(4301, "Client event error: Client events can only be sent to private or presence channels")),
        ({"event": "client-x", "channel": "private-other", "data": {}},
         error(4301, f"Client event error: Socket {a.socket_id} is not subscribed to channel private-other")),
        ({"event": "client-x", "data": {}}, error(4301, "Client event error: Channel required for client event")),
        ({"event": "client-" + "x" * 200, "channel": "private-r", "data": {}},
         error(4000, "Invalid event name: Event name exceeds maximum length of 200")),
    ]
    for frame, expected in cases:
        a.send(frame)
        assert frames(a) == [expected], frame
    assert frames(b) == []
    a.send({"event": "pusher:unsubscribe", "data": {"channel": "private-r"}})
    a.send({"event": "client-late", "channel": "private-r", "data": {}})
    assert frames(a) == [error(4301, f"Client event error: Socket {a.socket_id} is not subscribed to channel private-r")]
    assert frames(b, 0.3) == []

    closed = shared.app("noclient", enable_client_messages=False)
    c = s.connect(closed)
    c.join("private-z", closed)
    c.send({"event": "client-x", "channel": "private-z", "data": {}})
    assert frames(c) == [error(4301, "Client event error: Client events are not enabled for this app")]
    assert c.closed is None
    for client in (a, b, c):
        client.close()


def test_client_event_limits(shared):
    s = shared.server
    app = shared.app("climit", max_client_events_per_second=3, max_event_name_length=12, max_event_payload_in_kb=1)
    a, b = s.connect(app), s.connect(app)
    a.join("private-l", app)
    b.join("private-l", app)
    for i in range(6):
        a.send({"event": "client-r", "channel": "private-l", "data": {"i": i}})
    assert frames(a, 1.0) == [error(4301, "Client event rejected due to rate limit")] * 3
    assert same(frames(b), [{"event": "client-r", "channel": "private-l", "data": {"i": i}} for i in range(3)])
    time.sleep(1.2)
    a.send({"event": "client-" + "x" * 6, "channel": "private-l", "data": {}})
    assert frames(a) == [error(4000, "Invalid event name: Event name exceeds maximum length of 12")]
    a.send({"event": "client-" + "x" * 5, "channel": "private-l", "data": {}})
    assert frames(b) == [{"event": "client-xxxxx", "channel": "private-l", "data": {}}]
    time.sleep(1.2)
    a.send({"event": "client-p", "channel": "private-l", "data": "z" * 1100})
    assert frames(a) == [error(4301, "Client event error: Event payload size (1102 bytes) exceeds limit (1KB)")]
    assert frames(b, 0.3) == [] and a.closed is None
    a.close()
    b.close()
