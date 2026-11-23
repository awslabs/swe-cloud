"""The signed HTTP API: authentication, publishing and channel queries (one server for the module)."""

import time
import urllib.parse

from realtime import decoded, same, same_multiset, signed_query, wait_for


def err(status, code, message):
    return {"status": status, "code": code, "error": message}


def published(client, wait=1.0):
    """Channel events received by a client (protocol frames excluded), then marked consumed."""
    frames = [f for f in client.pending(wait) if not f["event"].startswith("pusher")]
    client.consume_all()
    return frames


def post(server, app, path, body, **kw):
    return server.api(app, "POST", f"/apps/{app.id}/{path}", body, **kw)


def get(server, app, path, params=None, **kw):
    return server.api(app, "GET", f"/apps/{app.id}{path}", params=params, **kw)


def ok(response):
    assert response.status == 200, response
    body = response.json()
    assert same(body, {"ok": True}), body


# ── authentication ──────────────────────────────────────────────────────────────


def test_signed_requests_are_accepted_and_bad_signatures_refused(shared):
    s, app = shared.server, shared.app("auth")
    other = shared.app("other")
    path = f"/apps/{app.id}/channels"
    r = get(s, app, "/channels")
    assert r.status == 200 and same(r.json(), {"channels": {}}), r

    cases = [
        (dict(secret="not-the-secret"), 401, "auth_failed", "Invalid API signature"),
        (dict(key="no-such-key"), 404, "app_not_found", "Application key not found or invalid: Invalid app key"),
        (dict(timestamp=int(time.time()) - 700), 401, "auth_failed", "Timestamp expired or too far in the future"),
        (dict(timestamp=int(time.time()) + 700), 401, "auth_failed", "Timestamp expired or too far in the future"),
        (dict(timestamp="soon"), 401, "auth_failed", "Invalid auth_timestamp format"),
        (dict(params={"body_md5": "d41d8cd98f00b204e9800998ecf8427e"}), 401, "auth_failed",
         "body_md5 must not be present in query parameters for GET requests"),
    ]
    for sign, status, code, message in cases:
        r = s.api(app, "GET", path, **sign)
        assert r.status == status and same(r.json(), err(status, code, message)), (sign, r)

    # A timestamp inside the tolerance window is fine; auth_version is not checked.
    assert get(s, app, "/channels", timestamp=int(time.time()) - 500).status == 200
    query = signed_query(app, "GET", path, params={"auth_version": "2.0"})
    assert s.request("GET", path + "?" + urllib.parse.urlencode(query)).status == 200

    # Signatures are lower-case hex over every parameter.
    query = signed_query(app, "GET", path)
    query["auth_signature"] = query["auth_signature"].upper()
    r = s.request("GET", path + "?" + urllib.parse.urlencode(query))
    assert r.status == 401 and same(r.json(), err(401, "auth_failed", "Invalid API signature")), r
    query = signed_query(app, "GET", path, params={"filter_by_prefix": "a"})
    query["filter_by_prefix"] = "b"
    r = s.request("GET", path + "?" + urllib.parse.urlencode(query))
    assert r.status == 401 and same(r.json(), err(401, "auth_failed", "Invalid API signature")), r
    query.pop("auth_signature")
    r = s.request("GET", path + "?" + urllib.parse.urlencode(query))
    assert r.status == 401 and same(r.json(), err(401, "auth_failed", "Invalid API signature")), r
    # The path is part of the signed string.
    query = signed_query(app, "GET", path)
    r = s.request("GET", path + "/room-1?" + urllib.parse.urlencode(query))
    assert r.status == 401 and same(r.json(), err(401, "auth_failed", "Invalid API signature")), r
    query = signed_query(app, "POST", path)
    r = s.request("GET", path + "?" + urllib.parse.urlencode(query))
    assert r.status == 401 and same(r.json(), err(401, "auth_failed", "Invalid API signature")), r

    # Unsigned requests.
    r = s.request("GET", path)
    assert r.status == 400 and same(r.json(), err(400, "invalid_input", "Missing authentication query parameters")), r
    r = s.request("GET", path + "?")
    assert r.status == 400 and same(r.json(), err(400, "invalid_input", "Query string is empty or invalid")), r

    # The key decides the app; it must match the app in the path.
    for target in (f"/apps/{other.id}/channels", "/apps/missing-app/channels"):
        r = s.api(app, "GET", target)
        assert r.status == 401 and same(r.json(), err(401, "auth_failed", "Authenticated app does not match route app_id")), r


def test_post_requests_require_a_matching_body_md5(shared):
    s, app = shared.server, shared.app("md5")
    c = s.connect(app)
    c.join("md5-room")
    body = {"name": "evt", "channel": "md5-room", "data": "payload"}
    r = post(s, app, "events", body, md5=False)
    assert r.status == 401 and same(r.json(), err(401, "auth_failed",
        "body_md5 is required and must be non-empty in query parameters for POST requests with a non-empty body")), r
    r = post(s, app, "events", body, md5=False, params={"body_md5": "0" * 32})
    assert r.status == 401 and same(r.json(), err(401, "auth_failed", "body_md5 mismatch")), r
    assert published(c, 1.0) == []
    ok(post(s, app, "events", body))
    assert published(c) == [{"event": "evt", "channel": "md5-room", "data": "payload"}]
    c.close()


# ── publishing ──────────────────────────────────────────────────────────────────


def test_publish_to_channels_and_data_encoding(shared):
    s, app = shared.server, shared.app("pub")
    a, b, outsider = s.connect(app), s.connect(app), s.connect(app)
    for client in (a, b):
        client.join("room-1")
        client.join("room-2")
    outsider.join("room-3")

    ok(post(s, app, "events", {"name": "greeting", "channel": "room-1", "data": "hello"}))
    for client in (a, b):
        assert published(client) == [{"event": "greeting", "channel": "room-1", "data": "hello"}]

    ok(post(s, app, "events", {"name": "multi", "channels": ["room-1", "room-2"], "data": "both"}))
    for client in (a, b):
        got = published(client)
        assert same_multiset(got, [{"event": "multi", "channel": "room-1", "data": "both"},
                                   {"event": "multi", "channel": "room-2", "data": "both"}]), got

    # Non-string data is delivered JSON-encoded; missing data becomes "null".
    ok(post(s, app, "events", {"name": "obj", "channel": "room-2", "data": {"a": 1, "b": [True, None, 1.5]}}))
    ok(post(s, app, "events", {"name": "num", "channel": "room-2", "data": 42}))
    ok(post(s, app, "events", {"name": "none", "channel": "room-2"}))
    got = published(a)
    assert [f["event"] for f in got] == ["obj", "num", "none"] and all(f["channel"] == "room-2" for f in got), got
    assert same([decoded(f)["data"] for f in got], [{"a": 1, "b": [True, None, 1.5]}, 42, None]), got
    assert all(set(f) == {"event", "channel", "data"} for f in got)
    assert same(published(b), got)

    # "channels" wins over "channel" when both are given.
    ok(post(s, app, "events", {"name": "pick", "channel": "room-2", "channels": ["room-1"], "data": "x"}))
    assert published(b) == [{"event": "pick", "channel": "room-1", "data": "x"}]
    assert published(outsider, 0.3) == []
    for client in (a, b, outsider):
        client.close()


def test_socket_id_excludes_only_that_connection(shared):
    s, app = shared.server, shared.app("excl")
    a, b = s.connect(app), s.connect(app)
    for client in (a, b):
        client.join("excl-room")
    ok(post(s, app, "events", {"name": "e", "channel": "excl-room", "data": "1", "socket_id": a.socket_id}))
    r = post(s, app, "batch_events", {"batch": [{"name": "f", "channel": "excl-room", "data": "2", "socket_id": b.socket_id}]})
    assert r.status == 200 and same(r.json(), {}), r
    # An unparseable socket id excludes nobody.
    ok(post(s, app, "events", {"name": "g", "channel": "excl-room", "data": "3", "socket_id": "not-a-socket"}))
    assert published(a) == [{"event": "f", "channel": "excl-room", "data": "2"}, {"event": "g", "channel": "excl-room", "data": "3"}]
    assert published(b) == [{"event": "e", "channel": "excl-room", "data": "1"}, {"event": "g", "channel": "excl-room", "data": "3"}]
    a.close()
    b.close()


def test_publish_info_reports_channel_attributes(shared):
    s, app = shared.server, shared.app("info")
    a, b = s.connect(app), s.connect(app)
    a.join("info-room")
    b.join("info-room")
    a.join("presence-info", app, "u1")
    b.join("presence-info", app, "u2")
    third = s.connect(app)
    third.join("presence-info", app, "u1")

    r = post(s, app, "events", {"name": "i", "channels": ["info-room", "presence-info", "info-empty"], "data": "x",
                                "info": "subscription_count,user_count"})
    assert r.status == 200, r
    channels = r.json()["channels"]
    assert set(channels) == {"info-room", "presence-info", "info-empty"}, channels
    ids = [channels[name].pop("acknowledgement_id") for name in sorted(channels)]
    assert all(isinstance(i, str) and i for i in ids) and len(set(ids)) == 3, ids
    assert same(channels, {"info-room": {"subscription_count": 2}, "presence-info": {"subscription_count": 3, "user_count": 2},
                           "info-empty": {"subscription_count": 0}}), channels

    r = post(s, app, "events", {"name": "i", "channel": "info-room", "data": "x", "info": ""})
    body = r.json()
    assert r.status == 200 and set(body) == {"channels"} and set(body["channels"]) == {"info-room"}, body
    assert set(body["channels"]["info-room"]) == {"acknowledgement_id"}, body
    for client in (a, b, third):
        client.close()


def test_event_validation_errors(shared):
    s, app = shared.server, shared.app("val")
    c = s.connect(app)
    c.join("room-1")
    cases = [
        ({"channel": "room-1", "data": "x"}, 400, "invalid_input", "Event name is required"),
        ({"name": "n", "data": "x"}, 400, "missing_channel_info", "Request must contain 'channels' (list) or 'channel' (string)"),
        ({"name": "n", "channels": [], "data": "x"}, 400, "missing_channel_info", "Request must contain 'channels' (list) or 'channel' (string)"),
        ({"name": "n", "channel": "bad channel!", "data": "x"}, 400, "invalid_input",
         "Channel name contains invalid characters: 'bad channel!' (invalid chars: [' ', '!'])"),
        ({"name": "n", "channel": "c" * 201, "data": "x"}, 400, "invalid_input", "Channel name too long. Max length is 200"),
        ({"name": "n" * 201, "channel": "room-1", "data": "x"}, 400, "limit_exceeded",
         f"Event name '{'n' * 201}' exceeds maximum length of 200"),
        ({"name": "n", "channel": "ns:thing", "data": "x"}, 400, "invalid_input", "Unknown channel namespace 'ns'"),
    ]
    for body, status, code, message in cases:
        r = post(s, app, "events", body)
        assert r.status == status and same(r.json(), err(status, code, message)), (body, r)
    assert published(c, 1.0) == []

    # Boundary values are accepted.
    ok(post(s, app, "events", {"name": "n" * 200, "channel": "room-1", "data": "edge"}))
    ok(post(s, app, "events", {"name": "long", "channel": "c" * 200, "data": "edge"}))
    assert published(c) == [{"event": "n" * 200, "channel": "room-1", "data": "edge"}]

    # Channels of one event are handled independently: the valid one is still delivered.
    r = post(s, app, "events", {"name": "partial", "channels": ["room-1", "bad channel"], "data": "p"})
    assert r.status == 400 and same(r.json(), err(400, "invalid_input",
        "Channel name contains invalid characters: 'bad channel' (invalid chars: [' '])")), r
    assert published(c) == [{"event": "partial", "channel": "room-1", "data": "p"}]

    # Malformed bodies are refused before publishing.
    r = post(s, app, "events", {"name": 5, "channel": "room-1", "data": "x"})
    assert r.status == 422, r
    r = post(s, app, "events", None, raw=b"{not json")
    assert r.status == 400, r
    r = post(s, app, "events", {"name": "n", "channel": "room-1", "data": "x"}, content_type="text/plain")
    assert r.status == 415, r
    assert published(c, 0.5) == []
    c.close()


def test_app_limits_apply_to_published_events(shared):
    s = shared.server
    app = shared.app("lim", max_channel_name_length=20, max_event_channels_at_once=2, max_event_name_length=10,
                     max_event_payload_in_kb=1, max_event_batch_size=2)
    c = s.connect(app)
    c.join("a")
    c.join("b")
    cases = [
        ({"name": "n", "channels": ["a", "b", "c"], "data": "x"}, 400, "limit_exceeded", "Number of channels (3) exceeds limit (2)"),
        ({"name": "n" * 11, "channel": "a", "data": "x"}, 400, "limit_exceeded", f"Event name '{'n' * 11}' exceeds maximum length of 10"),
        ({"name": "n", "channel": "c" * 21, "data": "x"}, 400, "invalid_input", "Channel name too long. Max length is 20"),
        ({"name": "n", "channel": "a", "data": "x" * 1025}, 413, "payload_too_large", "Event payload size (1025 bytes) for event 'n' exceeds limit (1KB)"),
        ({"name": "n", "channel": "a", "data": {"k": "x" * 1020}}, 413, "payload_too_large", "Event payload size (1028 bytes) for event 'n' exceeds limit (1KB)"),
    ]
    for body, status, code, message in cases:
        r = post(s, app, "events", body)
        assert r.status == status and same(r.json(), err(status, code, message)), (body, r)
    r = post(s, app, "batch_events", {"batch": [{"name": "x", "channel": "a", "data": str(i)} for i in range(3)]})
    assert r.status == 400 and same(r.json(), err(400, "limit_exceeded", "Batch size (3) exceeds limit (2)")), r
    assert published(c, 1.0) == []

    ok(post(s, app, "events", {"name": "n" * 10, "channels": ["a", "b"], "data": "x" * 1024}))
    r = post(s, app, "batch_events", {"batch": [{"name": "x", "channel": "a", "data": "1"}, {"name": "y", "channel": "b", "data": "2"}]})
    assert r.status == 200 and same(r.json(), {}), r
    got = published(c)
    assert same_multiset(got, [{"event": "n" * 10, "channel": "a", "data": "x" * 1024}, {"event": "n" * 10, "channel": "b", "data": "x" * 1024},
                               {"event": "x", "channel": "a", "data": "1"}, {"event": "y", "channel": "b", "data": "2"}]), got
    c.close()


def test_batch_events(shared):
    s, app = shared.server, shared.app("batch")
    a = s.connect(app)
    a.join("room-1")
    a.join("room-2")
    a.join("presence-b", app, "u1")

    r = post(s, app, "batch_events", {"batch": [{"name": "b1", "channel": "room-1", "data": "one"},
                                                {"name": "b2", "channel": "room-2", "data": {"o": 2}},
                                                {"name": "b3", "channels": ["room-1", "room-2"], "data": "multi"}]})
    assert r.status == 200 and same(r.json(), {}), r
    got = published(a)
    assert same_multiset([decoded(f) if f["event"] == "b2" else f for f in got], [
        {"event": "b1", "channel": "room-1", "data": "one"}, {"event": "b2", "channel": "room-2", "data": {"o": 2}},
        {"event": "b3", "channel": "room-1", "data": "multi"}, {"event": "b3", "channel": "room-2", "data": "multi"}]), got

    r = post(s, app, "batch_events", {"batch": [{"name": "i1", "channel": "room-1", "data": "x", "info": "subscription_count"},
                                                {"name": "i2", "channel": "presence-b", "data": "y", "info": "user_count,subscription_count"},
                                                {"name": "i3", "channel": "room-2", "data": "z"}]})
    assert r.status == 200, r
    batch = r.json()["batch"]
    assert len(batch) == 3 and all(isinstance(e.pop("acknowledgement_id", None), str) for e in batch[:2]), batch
    assert same(batch, [{"subscription_count": 1}, {"subscription_count": 1, "user_count": 1}, {}]), batch
    published(a)

    r = post(s, app, "batch_events", {"batch": []})
    assert r.status == 200 and same(r.json(), {}), r

    # Events are processed in order; the first invalid one stops the rest.
    r = post(s, app, "batch_events", {"batch": [{"name": "p1", "channel": "room-1", "data": "ok"},
                                                {"name": "p2", "channel": "bad chan", "data": "no"},
                                                {"name": "p3", "channel": "room-1", "data": "after"}]})
    assert r.status == 400 and same(r.json(), err(400, "invalid_input",
        "Channel name contains invalid characters: 'bad chan' (invalid chars: [' '])")), r
    assert published(a) == [{"event": "p1", "channel": "room-1", "data": "ok"}]
    a.close()


# ── queries ─────────────────────────────────────────────────────────────────────


def test_channel_listing(shared):
    s, app = shared.server, shared.app("list")
    a, b = s.connect(app), s.connect(app)
    for client in (a, b):
        client.join("room-1")
    a.join("room-2")
    a.join("private-p", app)
    a.join("presence-x", app, "u1")
    b.join("presence-x", app, "u2")
    c = s.connect(app)
    c.join("presence-x", app, "u1")

    def listing(params=None):
        r = get(s, app, "/channels", params)
        return r.status, r.json()

    assert same(listing(), (200, {"channels": {"room-1": {}, "room-2": {}, "private-p": {}, "presence-x": {}}}))
    assert same(listing({"filter_by_prefix": "room-"}), (200, {"channels": {"room-1": {}, "room-2": {}}}))
    assert same(listing({"info": "subscription_count"}), (200, {"channels": {
        "room-1": {"subscription_count": 2}, "room-2": {"subscription_count": 1}, "private-p": {"subscription_count": 1},
        "presence-x": {"subscription_count": 3}}}))
    assert same(listing({"filter_by_prefix": "presence-", "info": "user_count"}), (200, {"channels": {"presence-x": {"user_count": 2}}}))
    assert same(listing({"filter_by_prefix": "presence-", "info": "user_count,subscription_count"}),
                (200, {"channels": {"presence-x": {"user_count": 2, "subscription_count": 3}}}))
    assert same(listing({"info": "user_count"}), (400, err(400, "invalid_input",
                "user_count is only available for presence channels. Use filter_by_prefix=presence-")))
    assert same(listing({"filter_by_prefix": "zzz"}), (200, {"channels": {}}))
    assert same(listing({"filter_by_prefix": "room-", "info": "unknown"}), (200, {"channels": {}}))
    for client in (a, b, c):
        client.close()


def test_single_channel_info_and_users(shared):
    s, app = shared.server, shared.app("one")
    a, b, c = s.connect(app), s.connect(app), s.connect(app)
    a.join("room-1")
    b.join("room-1")
    a.join("presence-y", app, "u1", {"n": 1})
    b.join("presence-y", app, "u2")
    c.join("presence-y", app, "u1")

    def info(channel, params=None):
        r = get(s, app, f"/channels/{channel}", params)
        return r.status, r.json()

    assert same(info("room-1"), (200, {"occupied": True}))
    assert same(info("room-1", {"info": "subscription_count"}), (200, {"occupied": True, "subscription_count": 2}))
    assert same(info("room-1", {"info": "user_count"}), (400, err(400, "invalid_input", "user_count is only available for presence channels")))
    assert same(info("presence-y", {"info": "user_count,subscription_count"}), (200, {"occupied": True, "user_count": 2, "subscription_count": 3}))
    assert same(info("presence-y", {"info": "user_count"}), (200, {"occupied": True, "user_count": 2}))
    assert same(info("vacant"), (200, {"occupied": False}))
    assert same(info("vacant", {"info": "subscription_count"}), (200, {"occupied": False, "subscription_count": 0}))
    assert same(info("presence-vacant", {"info": "user_count"}), (200, {"occupied": False, "user_count": 0}))
    assert same(info("bad%20chan"), (400, err(400, "invalid_input", "Channel name contains invalid characters: 'bad chan' (invalid chars: [' '])")))

    r = get(s, app, "/channels/presence-y/users")
    assert r.status == 200 and set(r.json()) == {"users"}, r
    assert same_multiset(r.json()["users"], [{"id": "u1"}, {"id": "u2"}]), r
    r = get(s, app, "/channels/presence-vacant/users")
    assert r.status == 200 and same(r.json(), {"users": []}), r
    r = get(s, app, "/channels/room-1/users")
    assert r.status == 400 and same(r.json(), err(400, "invalid_input", "Only presence channels support this endpoint")), r

    # Leaving updates the counts and the member list.
    c.close()
    b.close()
    left = wait_for(lambda: same(get(s, app, "/channels/presence-y/users").json(), {"users": [{"id": "u1"}]}), 5)
    assert left, get(s, app, "/channels/presence-y/users")
    assert same(info("room-1", {"info": "subscription_count"}), (200, {"occupied": True, "subscription_count": 1}))
    assert same(info("presence-y", {"info": "user_count,subscription_count"}), (200, {"occupied": True, "user_count": 1, "subscription_count": 1}))
    a.close()


def test_disabled_app_still_uses_the_http_api(shared):
    s, app = shared.server, shared.app("dis", enabled=False)
    ok(post(s, app, "events", {"name": "n", "channel": "room", "data": "x"}))
    r = get(s, app, "/channels")
    assert r.status == 200 and same(r.json(), {"channels": {}}), r


def test_unknown_routes(shared):
    s, app = shared.server, shared.app("route")
    r = get(s, app, "/nonexistent")
    assert r.status == 404 and r.body == b"404 NOT FOUND", r
    r = s.request("GET", "/nothing-here")
    assert r.status == 404 and r.body == b"404 NOT FOUND", r
