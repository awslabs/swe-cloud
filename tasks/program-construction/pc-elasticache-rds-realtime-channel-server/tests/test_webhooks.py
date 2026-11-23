"""Webhook generation, filtering, batching, signing and delivery retries (a deployment per test)."""

import json
import time

import pytest
from realtime import ALL_EVENTS, decoded, hmac_hex, same, same_multiset, wait_for

NO_BATCH = {"webhooks.batching": {"enabled": False}}


def check_requests(requests, app, extra_headers=None):
    """Every delivery is a signed JSON POST carrying {time_ms, events}."""
    now = time.time() * 1000
    for r in requests:
        h = r["headers"]
        assert r["method"] == "POST"
        assert h.get("content-type") == "application/json", h
        assert h.get("x-pusher-key") == app.key, h
        assert h.get("x-pusher-signature") == hmac_hex(app.secret, r["body"]), h
        for name, value in (extra_headers or {}).items():
            assert h.get(name.lower()) == value, h
        body = json.loads(r["body"])
        assert set(body) == {"time_ms", "events"} and isinstance(body["events"], list) and body["events"], body
        assert type(body["time_ms"]) in (int, float) and abs(body["time_ms"] - now) < 120_000, body


def events_of(requests):
    return [e for r in requests for e in json.loads(r["body"])["events"]]


def settle(sink, path=None, quiet=1.5, timeout=20):
    """Wait until no new webhook request has arrived for `quiet` seconds."""
    end = time.monotonic() + timeout
    count = -1
    while time.monotonic() < end:
        current = len(sink.snapshot(path))
        if current == count:
            return sink.snapshot(path)
        count = current
        time.sleep(quiet)
    return sink.snapshot(path)


def test_channel_member_and_client_event_webhooks(deploy):
    d = deploy()
    s = d.start()
    headers = {"X-Custom": "v1", "Authorization": "Bearer t0ken"}
    app = d.app("life", webhooks=[{"url": d.sink.url("/all"), "event_types": ALL_EVENTS, "headers": headers}])
    s.wait_healthy()
    a, b = s.connect(app), s.connect(app)
    for client in (a, b):
        client.join("room-a")
    a.join("presence-a", app, "u1", {"n": 1})
    b.join("presence-a", app, "u2")
    a.join("private-a", app)
    b.join("private-a", app)
    a.send({"event": "client-obj", "channel": "private-a", "data": {"k": [1, 2.5, True, None]}})
    a.send({"event": "client-str", "channel": "private-a", "data": "text"})
    b.send({"event": "client-pres", "channel": "presence-a", "data": {"p": 1}})
    # Server-published events are not webhook events.
    assert s.api(app, "POST", f"/apps/{app.id}/events", {"name": "srv", "channel": "room-a", "data": "x"}).status == 200

    expected = [
        {"name": "channel_occupied", "channel": "room-a"},
        {"name": "channel_occupied", "channel": "presence-a"},
        {"name": "channel_occupied", "channel": "private-a"},
        {"name": "member_added", "channel": "presence-a", "user_id": "u1"},
        {"name": "member_added", "channel": "presence-a", "user_id": "u2"},
        {"name": "client_event", "channel": "private-a", "event": "client-obj", "data": {"k": [1, 2.5, True, None]}, "socket_id": a.socket_id},
        {"name": "client_event", "channel": "private-a", "event": "client-str", "data": "text", "socket_id": a.socket_id},
        {"name": "client_event", "channel": "presence-a", "event": "client-pres", "data": {"p": 1}, "socket_id": b.socket_id},
    ]
    wait_for(lambda: len(d.sink.events()) >= len(expected), 10)
    requests = settle(d.sink)
    check_requests(requests, app, headers)
    assert same_multiset(events_of(requests), expected), events_of(requests)
    assert all(r["path"] == "/all" for r in requests)

    count = len(requests)
    a.send({"event": "pusher:unsubscribe", "data": {"channel": "room-a"}})
    b.close()
    a.close()
    gone = [
        {"name": "channel_vacated", "channel": "room-a"},
        {"name": "channel_vacated", "channel": "presence-a"},
        {"name": "channel_vacated", "channel": "private-a"},
        {"name": "member_removed", "channel": "presence-a", "user_id": "u1"},
        {"name": "member_removed", "channel": "presence-a", "user_id": "u2"},
    ]
    wait_for(lambda: len(d.sink.events()) >= len(expected) + len(gone), 15)
    requests = settle(d.sink)
    check_requests(requests, app, headers)
    assert same_multiset(events_of(requests[count:]), gone), events_of(requests[count:])
    assert wait_for(lambda: d.queue_counts() == (0, 0), 10), d.queue_counts()


def test_member_webhooks_track_users_not_connections(deploy):
    d = deploy()
    s = d.start()
    app = d.app("users", webhooks=[{"url": d.sink.url(), "event_types": ["member_added", "member_removed"]}])
    s.wait_healthy()
    a, b, c = s.connect(app), s.connect(app), s.connect(app)
    a.join("presence-d", app, "dup")
    b.join("presence-d", app, "dup", {"second": True})
    c.join("presence-d", app, "other")
    wait_for(lambda: len(d.sink.events()) >= 2, 10)
    time.sleep(1)
    assert same_multiset(d.sink.events(), [{"name": "member_added", "channel": "presence-d", "user_id": "dup"},
                                           {"name": "member_added", "channel": "presence-d", "user_id": "other"}]), d.sink.events()
    a.close()
    time.sleep(4.5)
    assert len(d.sink.events()) == 2, d.sink.events()
    b.close()
    assert wait_for(lambda: len(d.sink.events()) >= 3, 10)
    time.sleep(1)
    assert same(d.sink.events()[2:], [{"name": "member_removed", "channel": "presence-d", "user_id": "dup"}]), d.sink.events()
    c.close()


def test_vacancy_is_confirmed_before_channel_vacated(deploy):
    d = deploy()
    s = d.start(sections=NO_BATCH)
    app = d.app("vacant", webhooks=[{"url": d.sink.url(), "event_types": ["channel_occupied", "channel_vacated"]}])
    s.wait_healthy()
    c = s.connect(app)
    c.join("solo")
    assert wait_for(lambda: len(d.sink.events()) == 1, 10)
    # Leaving and coming back within a moment: no vacancy is reported, but occupancy is reported again.
    c.send({"event": "pusher:unsubscribe", "data": {"channel": "solo"}})
    time.sleep(1)
    c.join("solo")
    time.sleep(5)
    occupied, vacated = {"name": "channel_occupied", "channel": "solo"}, {"name": "channel_vacated", "channel": "solo"}
    assert same(d.sink.events(), [occupied, occupied]), d.sink.events()
    # Staying away long enough reports the vacancy.
    c.send({"event": "pusher:unsubscribe", "data": {"channel": "solo"}})
    assert wait_for(lambda: len(d.sink.events()) == 3, 10)
    c.join("solo")
    assert wait_for(lambda: len(d.sink.events()) == 4, 10)
    time.sleep(1)
    assert same(d.sink.events(), [occupied, occupied, vacated, occupied]), d.sink.events()
    check_requests(d.sink.snapshot(), app)
    c.close()


def test_webhook_targets_select_events_and_channels(deploy):
    d = deploy()
    s = d.start(sections=NO_BATCH)
    hooks = [
        {"url": d.sink.url("/occupied"), "event_types": ["channel_occupied"]},
        {"url": d.sink.url("/members"), "event_types": ["member_added", "member_removed", "channel_occupied"], "filter": {"channel_prefix": "presence-"}},
        {"url": d.sink.url("/suffix"), "event_types": ALL_EVENTS, "filter": {"channel_suffix": "-x"}},
        {"url": d.sink.url("/pattern"), "event_types": ["channel_occupied", "channel_vacated"], "filter": {"channel_pattern": "^room-[0-9]+$"}},
        {"url": d.sink.url("/both"), "event_types": ["channel_occupied"], "filter": {"channel_prefix": "room-", "channel_suffix": "-x"}},
        {"url": d.sink.url("/none"), "event_types": []},
        {"url": d.sink.url("/badregex"), "event_types": ["channel_occupied"], "filter": {"channel_pattern": "(["}},
    ]
    app = d.app("filters", webhooks=hooks)
    s.wait_healthy()
    c = s.connect(app)
    for channel in ("room-1", "room-x", "room-abc", "other-x"):
        c.join(channel)
    for channel in ("presence-x", "presence-room-1"):
        c.join(channel, app, "u")
    c.join("private-q-x", app)
    c.send({"event": "client-z", "channel": "private-q-x", "data": {"z": 1}})
    time.sleep(2)
    c.close()
    wait_for(lambda: len(d.sink.snapshot("/members")) >= 6 and len(d.sink.snapshot("/suffix")) >= 11, 20)
    settle(d.sink, quiet=2)

    def at(path):
        requests = d.sink.snapshot(path)
        check_requests(requests, app)
        # Without batching every request carries one event.
        assert all(len(json.loads(r["body"])["events"]) == 1 for r in requests), path
        return events_of(requests)

    occ = lambda ch: {"name": "channel_occupied", "channel": ch}  # noqa: E731
    vac = lambda ch: {"name": "channel_vacated", "channel": ch}  # noqa: E731
    assert same_multiset(at("/occupied"), [occ(ch) for ch in ("room-1", "room-x", "room-abc", "other-x", "presence-x", "presence-room-1", "private-q-x")])
    assert same_multiset(at("/members"), [occ("presence-x"), occ("presence-room-1"),
                                          {"name": "member_added", "channel": "presence-x", "user_id": "u"},
                                          {"name": "member_added", "channel": "presence-room-1", "user_id": "u"},
                                          {"name": "member_removed", "channel": "presence-x", "user_id": "u"},
                                          {"name": "member_removed", "channel": "presence-room-1", "user_id": "u"}])
    assert same_multiset(at("/suffix"), [occ("room-x"), occ("other-x"), occ("presence-x"), occ("private-q-x"),
                                         vac("room-x"), vac("other-x"), vac("presence-x"), vac("private-q-x"),
                                         {"name": "member_added", "channel": "presence-x", "user_id": "u"},
                                         {"name": "member_removed", "channel": "presence-x", "user_id": "u"},
                                         {"name": "client_event", "channel": "private-q-x", "event": "client-z", "data": {"z": 1}, "socket_id": c.socket_id}])
    assert same_multiset(at("/pattern"), [occ("room-1"), vac("room-1")])
    assert same_multiset(at("/both"), [occ("room-x")])
    assert at("/none") == [] and at("/badregex") == []


@pytest.mark.parametrize("batching, expect", [
    ({"enabled": False}, "single"),
    ({"enabled": True, "duration": 1500, "size": 2}, "pairs"),
    ({"enabled": True, "duration": 1500, "size": 100}, "together"),
])
def test_batching_groups_events_into_requests(deploy, batching, expect):
    d = deploy()
    s = d.start(sections={"webhooks.batching": batching})
    app = d.app("batch", webhooks=[{"url": d.sink.url(), "event_types": ["channel_occupied"]}])
    s.wait_healthy()
    c = s.connect(app)
    for i in range(5):
        c.subscribe(f"ch-{i}")
    assert wait_for(lambda: len(d.sink.events()) >= 5, 10), d.sink.events()
    requests = settle(d.sink)
    check_requests(requests, app)
    sizes = sorted(len(json.loads(r["body"])["events"]) for r in requests)
    assert same_multiset(events_of(requests), [{"name": "channel_occupied", "channel": f"ch-{i}"} for i in range(5)])
    if expect == "single":
        assert sizes == [1] * 5, sizes
    elif expect == "pairs":
        assert len(sizes) == 3 and max(sizes) == 2, sizes
    else:
        # A single batching window, unless the burst happened to straddle a flush.
        assert len(sizes) <= 2 and max(sizes) >= 3, sizes
    c.close()


@pytest.mark.parametrize("statuses, retry, attempts", [
    ([500, 500], {"enabled": True, "max_attempts": 3, "initial_backoff_ms": 200, "max_backoff_ms": 1000}, 3),
    ([500] * 5, {"enabled": True, "max_attempts": 3, "initial_backoff_ms": 200, "max_backoff_ms": 1000}, 3),
    ([429], {"enabled": True, "max_attempts": 3, "initial_backoff_ms": 200}, 2),
    ([400, 400], {"enabled": True, "max_attempts": 3, "initial_backoff_ms": 200}, 1),
    ([404], {"enabled": True, "max_attempts": 3, "initial_backoff_ms": 200}, 1),
    ([500, 500], {"enabled": False}, 1),
    ([503] * 20, {"enabled": True, "initial_backoff_ms": 400, "max_backoff_ms": 400, "max_elapsed_time_ms": 1500}, 4),
], ids=["recovers", "gives-up", "throttled", "client-error", "not-found", "disabled", "elapsed"])
def test_delivery_retries(deploy, statuses, retry, attempts):
    d = deploy()
    d.sink.statuses.extend(statuses)
    s = d.start(sections={"webhooks.retry": retry, **NO_BATCH})
    app = d.app("retry", webhooks=[{"url": d.sink.url(), "event_types": ["channel_occupied"]}])
    s.wait_healthy()
    c = s.connect(app)
    c.join("r-1")
    assert wait_for(lambda: d.sink.snapshot(), 10)
    requests = settle(d.sink, quiet=1.5)
    assert len(requests) == attempts, [r["at"] for r in requests]
    check_requests(requests, app)
    assert len({r["body"] for r in requests}) == 1
    assert same(events_of(requests[:1]), [{"name": "channel_occupied", "channel": "r-1"}])
    # The queued job is finished either way: the message is deleted, not redelivered.
    assert wait_for(lambda: d.queue_counts() == (0, 0), 10), d.queue_counts()
    time.sleep(2)
    assert len(d.sink.snapshot()) == attempts
    c.close()


@pytest.mark.parametrize("retry, attempts, gaps", [
    ({"enabled": True, "initial_backoff_ms": 500, "max_backoff_ms": 2000, "max_attempts": 5}, 5, [0.5, 1.0, 2.0, 2.0]),
    ({"enabled": True, "initial_backoff_ms": 300, "max_backoff_ms": 700, "max_attempts": 6}, 6, [0.3, 0.6, 0.7, 0.7, 0.7]),
], ids=["doubling", "capped"])
def test_retry_backoff_doubles_up_to_the_cap(deploy, retry, attempts, gaps):
    d = deploy()
    d.sink.statuses.extend([503] * 10)
    s = d.start(sections={"webhooks.retry": retry, **NO_BATCH})
    app = d.app("backoff", webhooks=[{"url": d.sink.url(), "event_types": ["channel_occupied"]}])
    s.wait_healthy()
    c = s.connect(app)
    c.join("b-1")
    assert wait_for(lambda: len(d.sink.snapshot()) >= attempts, 15)
    requests = settle(d.sink, quiet=2.5)
    assert len(requests) == attempts, [r["at"] for r in requests]
    check_requests(requests, app)
    observed = [b["at"] - a["at"] for a, b in zip(requests, requests[1:])]
    assert all(0.75 * e < g < 1.35 * e + 0.15 for g, e in zip(observed, gaps)), (observed, gaps)
    assert wait_for(lambda: d.queue_counts() == (0, 0), 10), d.queue_counts()
    c.close()


def test_member_removed_is_confirmed_before_it_is_sent(deploy):
    d = deploy()
    s = d.start(sections=NO_BATCH)
    app = d.app("rejoin", webhooks=[{"url": d.sink.url(), "event_types": ["member_added", "member_removed"]}])
    s.wait_healthy()
    watcher, member = s.connect(app), s.connect(app)
    watcher.join("presence-r", app, "watch")
    member.join("presence-r", app, "u1")
    added = {"name": "member_added", "channel": "presence-r", "user_id": "u1"}
    removed = {"name": "member_removed", "channel": "presence-r", "user_id": "u1"}
    assert wait_for(lambda: len(d.sink.events()) >= 2, 10)
    watcher.pending(0.5)
    watcher.consume_all()

    # Leaving and rejoining within a moment: subscribers see both frames at once,
    # but the webhook only reports the new join.
    member.send({"event": "pusher:unsubscribe", "data": {"channel": "presence-r"}})
    time.sleep(0.5)
    member.join("presence-r", app, "u1")
    got = [decoded(f) for f in watcher.pending(1.0)]
    watcher.consume_all()
    assert same(got, [{"event": "pusher_internal:member_removed", "channel": "presence-r", "data": {"user_id": "u1"}},
                      {"event": "pusher_internal:member_added", "channel": "presence-r", "data": {"user_id": "u1", "user_info": {}}}]), got
    time.sleep(5)
    assert same_multiset(d.sink.events(), [{"name": "member_added", "channel": "presence-r", "user_id": "watch"}, added, added]), d.sink.events()

    # Staying away reports the removal.
    member.close()
    assert wait_for(lambda: len(d.sink.events()) >= 4, 12)
    time.sleep(1)
    assert same(d.sink.events()[3:], [removed]), d.sink.events()
    check_requests(d.sink.snapshot(), app)
    watcher.close()


def test_message_stays_in_flight_while_delivery_is_retried(deploy):
    d = deploy()
    d.sink.statuses.extend([503] * 4)
    s = d.start(sections={"webhooks.retry": {"enabled": True, "initial_backoff_ms": 2000, "max_backoff_ms": 2000, "max_elapsed_time_ms": 60000},
                          "queue.sqs": {"visibility_timeout": 4}, **NO_BATCH})
    app = d.app("vis", webhooks=[{"url": d.sink.url(), "event_types": ["channel_occupied"]}])
    s.wait_healthy()
    c = s.connect(app)
    t0 = time.monotonic()
    c.join("v-1")
    assert wait_for(lambda: d.sink.snapshot(), 10)
    observed = []
    while time.monotonic() - t0 < 6.5:
        observed.append((time.monotonic() - t0, d.queue_counts(), len(d.sink.snapshot())))
        time.sleep(0.5)
    # Longer than the visibility timeout, the one message is still held, not made visible again.
    assert all(counts == (0, 1) for _, counts, _ in observed[1:]), observed
    assert observed[-1][2] in (3, 4), observed
    assert wait_for(lambda: len(d.sink.snapshot()) >= 5, 10)
    assert wait_for(lambda: d.queue_counts() == (0, 0), 10), d.queue_counts()
    time.sleep(2)
    requests = d.sink.snapshot()
    assert len(requests) == 5 and len({r["body"] for r in requests}) == 1
    gaps = [b["at"] - a["at"] for a, b in zip(requests, requests[1:])]
    assert all(1.5 < g < 3.5 for g in gaps), gaps
    c.close()
