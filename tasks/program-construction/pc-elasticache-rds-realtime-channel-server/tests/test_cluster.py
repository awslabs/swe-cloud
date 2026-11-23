"""Valkey-backed state: several servers sharing an adapter prefix, and the app/listing caches."""

import json
import time

import psycopg
from realtime import App, decoded, same, same_multiset, wait_for


def frames(client, wait=1.0):
    out = client.pending(wait)
    client.consume_all()
    return out


def test_servers_sharing_a_prefix_act_as_one(deploy):
    d = deploy()
    one, two = d.start(), d.start()
    app = d.app("shared")
    one.wait_healthy()
    two.wait_healthy()
    a, b = one.connect(app), two.connect(app)
    a.join("shared")
    b.join("shared")
    a.join("presence-m", app, "n1")
    joined = b.join("presence-m", app, "n2", {"x": 1})
    data = decoded(joined)["data"]["presence"]
    assert same({**data, "ids": sorted(data["ids"])}, {"ids": ["n1", "n2"], "hash": {"n1": {}, "n2": {"x": 1}}, "count": 2}), data
    got = [decoded(f) for f in frames(a)]
    assert same(got, [{"event": "pusher_internal:member_added", "channel": "presence-m", "data": {"user_id": "n2", "user_info": {"x": 1}}}]), got

    # Publishing through either server reaches subscribers on both.
    assert one.api(app, "POST", f"/apps/{app.id}/events", {"name": "x", "channel": "shared", "data": "cross"}).status == 200
    assert frames(a) == [{"event": "x", "channel": "shared", "data": "cross"}]
    assert frames(b) == [{"event": "x", "channel": "shared", "data": "cross"}]
    assert two.api(app, "POST", f"/apps/{app.id}/events", {"name": "y", "channel": "shared", "data": "back", "socket_id": b.socket_id}).status == 200
    assert frames(a) == [{"event": "y", "channel": "shared", "data": "back"}] and frames(b, 0.3) == []

    # Client events cross servers too.
    a.join("private-m", app)
    b.join("private-m", app)
    a.send({"event": "client-hi", "channel": "private-m", "data": {"h": 1}})
    assert same(frames(b), [{"event": "client-hi", "channel": "private-m", "data": {"h": 1}}])
    assert frames(a, 0.3) == []

    # Queries aggregate every server's connections.
    a.join("only-one")
    r = one.api(app, "GET", f"/apps/{app.id}/channels/presence-m/users")
    assert same_multiset(r.json()["users"], [{"id": "n1"}, {"id": "n2"}]), r
    r = two.api(app, "GET", f"/apps/{app.id}/channels", params={"info": "subscription_count"})
    assert same(r.json(), {"channels": {"shared": {"subscription_count": 2}, "presence-m": {"subscription_count": 2},
                                        "private-m": {"subscription_count": 2}, "only-one": {"subscription_count": 1}}}), r
    r = two.api(app, "GET", f"/apps/{app.id}/channels/presence-m", params={"info": "user_count,subscription_count"})
    assert same(r.json(), {"occupied": True, "user_count": 2, "subscription_count": 2}), r

    # Leaving on one server is announced on the other.
    a.close()
    got = [decoded(f) for f in frames(b, 2.0)]
    assert same(got, [{"event": "pusher_internal:member_removed", "channel": "presence-m", "data": {"user_id": "n1"}}]), got
    assert wait_for(lambda: same(one.api(app, "GET", f"/apps/{app.id}/channels/only-one").json(), {"occupied": False}), 5)
    b.close()


def test_connection_limit_counts_every_server(deploy):
    d = deploy()
    one, two = d.start(), d.start()
    app = d.app("quota", max_connections=2)
    one.wait_healthy()
    two.wait_healthy()
    a, b = one.connect(app), two.connect(app)
    time.sleep(1)
    for server in (one, two):
        extra = server.ws(app.key)
        assert extra.handshake(5) is None and extra.frames == []
        assert extra.wait_closed(5) is not None
        extra.close()
    a.close()
    time.sleep(1.5)
    c = two.connect(app)
    c.close()
    b.close()


def test_different_prefixes_are_isolated(deploy):
    d = deploy()
    one = d.start()
    other = d.start(sections={"adapter.redis": {"prefix": "other" + d.tag}})
    app = d.app("iso")
    one.wait_healthy()
    other.wait_healthy()
    a, b = one.connect(app), other.connect(app)
    a.join("iso")
    b.join("iso")
    assert one.api(app, "POST", f"/apps/{app.id}/events", {"name": "x", "channel": "iso", "data": "local"}).status == 200
    assert frames(a) == [{"event": "x", "channel": "iso", "data": "local"}]
    assert frames(b, 0.5) == []
    r = one.api(app, "GET", f"/apps/{app.id}/channels/iso", params={"info": "subscription_count"})
    assert same(r.json(), {"occupied": True, "subscription_count": 1}), r
    a.close()
    b.close()


def cache_entries(d):
    r = d.engines.redis()
    return {k.decode(): (r.ttl(k), r.get(k)) for k in r.scan_iter(match=d.cache_prefix + "*")}


def test_resolved_apps_are_cached_in_valkey(deploy):
    d = deploy()
    s = d.start(sections={"app_manager.cache": {"enabled": True, "ttl": 30}})
    app = d.app("cached", max_connections=7)
    s.wait_healthy()
    c = s.connect(app)
    assert s.api(app, "GET", f"/apps/{app.id}/channels/x").status == 200
    entries = cache_entries(d)
    prefix = d.cache_prefix
    by_id, by_key = f"{prefix}:app:id:{app.id}", f"{prefix}:app:key:{app.key}"
    assert {by_id, by_key} <= set(entries), entries
    for name in (by_id, by_key):
        ttl, value = entries[name]
        assert 0 < ttl <= 30, (name, ttl)
        record = json.loads(value)
        assert same({k: record.get(k) for k in ("id", "key", "secret", "enabled")},
                    {"id": app.id, "key": app.key, "secret": app.secret, "enabled": True}), record
    c.close()

    d2 = deploy()
    s2 = d2.start(sections={"app_manager.cache": {"enabled": False}})
    other = d2.app("uncached")
    s2.wait_healthy()
    c = s2.connect(other)
    assert s2.api(other, "GET", f"/apps/{other.id}/channels").status == 200
    assert not any(":app:" in name for name in cache_entries(d2)), cache_entries(d2)
    c.close()


def test_app_changes_apply_when_the_cache_entry_expires(deploy):
    d = deploy()
    s = d.start(sections={"app_manager.cache": {"enabled": True, "ttl": 6}})
    app = d.app("stale")
    s.wait_healthy()
    key = f"{d.cache_prefix}:app:key:{app.key}"
    r = d.engines.redis()
    # Start from a freshly cached entry so the stale window is predictable.
    assert s.api(app, "GET", f"/apps/{app.id}/channels").status == 200
    if r.ttl(key) < 5:
        time.sleep(max(r.ttl(key), 0) + 1)
        assert s.api(app, "GET", f"/apps/{app.id}/channels").status == 200
    assert r.ttl(key) >= 5, r.ttl(key)
    with d.conn() as c:
        c.execute("UPDATE applications SET secret = 'rotated', enabled = false WHERE id = %s", (app.id,))
    rotated = App(app.id, app.key, "rotated")
    # Still served from the cache: old secret valid, connections accepted.
    assert s.api(app, "GET", f"/apps/{app.id}/channels").status == 200
    assert s.api(rotated, "GET", f"/apps/{app.id}/channels").status == 401
    c = s.ws(app.key)
    assert c.handshake()["event"] == "pusher:connection_established"
    c.close()
    # After expiry the row is read again.
    assert wait_for(lambda: s.api(rotated, "GET", f"/apps/{app.id}/channels").status == 200, 10)
    assert s.api(app, "GET", f"/apps/{app.id}/channels").status == 401
    c = s.ws(app.key)
    assert c.handshake(5) is None and c.wait_closed(5) is not None
    record = json.loads(r.get(key) or b"{}")
    assert same({k: record.get(k) for k in ("secret", "enabled")}, {"secret": "rotated", "enabled": False}), record


def test_app_changes_apply_immediately_without_the_cache(deploy):
    d = deploy()
    s = d.start(sections={"app_manager.cache": {"enabled": False}})
    app = d.app("live", webhooks=[{"url": d.sink.url("/old"), "event_types": ["channel_occupied"]}])
    s.wait_healthy()
    c = s.connect(app)
    c.join("x1")
    assert wait_for(lambda: d.sink.snapshot("/old"), 10)
    with d.conn() as conn:
        conn.execute("UPDATE applications SET secret = 'next', webhooks = %s WHERE id = %s",
                     (psycopg.types.json.Jsonb([{"url": d.sink.url("/new"), "event_types": ["channel_occupied"]}]), app.id))
    assert s.api(app, "GET", f"/apps/{app.id}/channels").status == 401
    assert s.api(App(app.id, app.key, "next"), "GET", f"/apps/{app.id}/channels").status == 200
    c.join("x2")
    assert wait_for(lambda: d.sink.snapshot("/new"), 10)
    assert same(d.sink.events("/new"), [{"name": "channel_occupied", "channel": "x2"}]), d.sink.events()
    assert same(d.sink.events("/old"), [{"name": "channel_occupied", "channel": "x1"}]), d.sink.events()
    c.close()


def test_channel_listing_is_cached_briefly(deploy):
    d = deploy()
    s = d.start()
    app = d.app("listing")
    s.wait_healthy()
    c = s.connect(app)
    c.join("one")
    listing = lambda params=None: s.api(app, "GET", f"/apps/{app.id}/channels", params=params).json()  # noqa: E731
    assert same(listing(), {"channels": {"one": {}}})
    c.join("two")
    assert same(listing(), {"channels": {"one": {}}})
    # Each filter/info combination is cached separately; single-channel info is never cached.
    assert same(listing({"filter_by_prefix": "t"}), {"channels": {"two": {}}})
    assert same(s.api(app, "GET", f"/apps/{app.id}/channels/two").json(), {"occupied": True})
    time.sleep(2.5)
    assert same(listing(), {"channels": {"one": {}, "two": {}}})
    c.close()
