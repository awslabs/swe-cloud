"""Bounce webhook gating, per-type count/action rules, validation, and the bounce records API."""

import time
from datetime import datetime, timezone

import pytest

from mailer import assert_json

BOUNCES_SQL = ("SELECT s.email, b.type::text, b.source, b.meta FROM bounces b JOIN subscribers s ON s.id = b.subscriber_id "
               "ORDER BY b.id")
STATE_SQL = ("SELECT s.email, s.status::text, ARRAY_AGG(sl.status::text ORDER BY sl.list_id) FROM subscribers s "
             "LEFT JOIN subscriber_lists sl ON sl.subscriber_id = s.id WHERE s.email LIKE '%%@b.test' GROUP BY 1, 2 ORDER BY 1")


def enable_bounces(settings, actions=None):
    settings["bounce.enabled"] = True
    settings["bounce.webhooks_enabled"] = True
    if actions:
        settings["bounce.actions"] = actions


def wait_for(check, timeout=10):
    """Bounces may be recorded asynchronously after the webhook answers."""
    deadline = time.time() + timeout
    while True:
        value = check()
        if value or time.time() > deadline:
            return value
        time.sleep(0.2)


class Bouncer:
    def __init__(self, app):
        self.app = app
        self.sentinels = 0

    def post(self, **body):
        assert self.app.ok("POST", "/webhooks/bounce", body) is True

    def settle(self):
        """Bounce a fresh sentinel subscriber and wait for its record, so earlier bounces have been processed."""
        self.sentinels += 1
        email = f"s{self.sentinels}@sentinel.test"
        self.app.create_subscriber(email)
        self.post(email=email, source="sentinel", type="soft")
        query = "SELECT COUNT(*) FROM bounces b JOIN subscribers s ON s.id = b.subscriber_id WHERE s.email = %s"
        assert wait_for(lambda: self.app.q(query, email)[0][0] == 1), "sentinel bounce never recorded"

    def records(self):
        return [r for r in self.app.q(BOUNCES_SQL) if not r[0].endswith("@sentinel.test")]


@pytest.fixture
def bouncing(app):
    lists = [app.create_list("B1")["id"], app.create_list("B2")["id"]]
    subs = {}
    for name in ("soft", "hard", "comp", "uns", "del", "uu", "none"):
        subs[name] = app.create_subscriber(f"{name}@b.test", lists=lists, preconfirm_subscriptions=True)
    return app, subs, Bouncer(app)


def test_webhook_requires_both_switches(app):
    sub = app.create_subscriber("gate@b.test")
    body = {"email": "gate@b.test", "source": "api", "type": "hard"}
    assert app.call("POST", "/webhooks/bounce", body)[0] == 404
    app.configure(lambda s: s.update({"bounce.webhooks_enabled": True}))
    assert app.call("POST", "/webhooks/bounce", body)[0] == 503
    assert app.call("POST", "/webhooks/bounce", body, auth=False)[0] == 403
    app.configure(lambda s: s.update({"bounce.webhooks_enabled": False, "bounce.enabled": True}))
    assert app.call("POST", "/webhooks/bounce", body)[0] == 404
    assert_json(dict(app.q("SELECT key, value FROM settings WHERE key IN ('bounce.enabled', 'bounce.webhooks_enabled')")),
                {"bounce.enabled": True, "bounce.webhooks_enabled": False})
    time.sleep(1)
    assert app.q("SELECT COUNT(*) FROM bounces") == [(0,)]
    assert app.q("SELECT status::text FROM subscribers WHERE id = %s", sub["id"]) == [("enabled",)]


def test_default_actions(bouncing):
    app, subs, bouncer = bouncing
    app.configure(enable_bounces)
    # soft: count 2, none. A bounce beyond the count is not recorded.
    for i in range(3):
        bouncer.post(email="soft@b.test", source="api", type="soft", meta={"i": i})
    # hard: count 1, blocklist. Bounces of a blocklisted subscriber are not recorded.
    bouncer.post(email="HARD@b.test", source="api", type="hard")
    bouncer.post(email="hard@b.test", source="api", type="hard")
    # complaint: count 1, blocklist.
    bouncer.post(email="comp@b.test", source="fbl", type="complaint", meta={"reason": "spam"}, created_at="2024-01-02T03:04:05Z")
    bouncer.settle()
    assert bouncer.records() == [
        ("soft@b.test", "soft", "api", {"i": 0}),
        ("soft@b.test", "soft", "api", {"i": 1}),
        ("hard@b.test", "hard", "api", {}),
        ("comp@b.test", "complaint", "fbl", {"reason": "spam"}),
    ]
    state = dict((r[0], (r[1], r[2])) for r in app.q(STATE_SQL))
    # Blocklisting by bounce leaves the memberships as they were.
    assert state["hard@b.test"] == ("blocklisted", ["confirmed", "confirmed"])
    assert state["comp@b.test"] == ("blocklisted", ["confirmed", "confirmed"])
    assert state["soft@b.test"] == ("enabled", ["confirmed", "confirmed"])
    created = app.q("SELECT b.created_at FROM bounces b WHERE b.type = 'complaint'")[0][0]
    assert created == datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def test_configured_actions(bouncing):
    app, subs, bouncer = bouncing
    actions = {"soft": {"count": 2, "action": "unsubscribe"}, "hard": {"count": 1, "action": "delete"}, "complaint": {"count": 3, "action": "none"}}
    app.configure(lambda s: enable_bounces(s, actions))
    assert_json(app.q("SELECT value FROM settings WHERE key = 'bounce.actions'")[0][0], actions)
    assert_json(app.settings()["bounce.actions"], actions)

    bouncer.post(email="uns@b.test", source="api", type="soft")
    bouncer.settle()
    assert app.q(STATE_SQL)[-2:] == [("uns@b.test", "enabled", ["confirmed", "confirmed"]), ("uu@b.test", "enabled", ["confirmed", "confirmed"])]
    bouncer.post(email="uns@b.test", source="api", type="soft")
    bouncer.post(email="uns@b.test", source="api", type="soft")
    for _ in range(4):
        bouncer.post(email="comp@b.test", source="api", type="complaint")
    bouncer.post(email="del@b.test", source="api", type="hard")
    bouncer.settle()
    state = dict((r[0], (r[1], r[2])) for r in app.q(STATE_SQL))
    assert state["uns@b.test"] == ("enabled", ["unsubscribed", "unsubscribed"])
    assert state["comp@b.test"] == ("enabled", ["confirmed", "confirmed"])
    assert "del@b.test" not in state
    status, _ = app.call("GET", f"/api/subscribers/{subs['del']['id']}")
    assert status == 400
    assert [(r[0], r[1]) for r in bouncer.records()] == [("uns@b.test", "soft")] * 2 + [("comp@b.test", "complaint")] * 3


def test_bounce_identification_and_validation(bouncing):
    app, subs, bouncer = bouncing
    app.configure(enable_bounces)
    campaign_uuid, campaign_id, campaign_name = app.q("SELECT uuid::text, id, name FROM campaigns ORDER BY id LIMIT 1")[0]
    rejected = [
        {"source": "api", "type": "hard"},
        {"email": "soft@b.test", "source": "api", "type": "bogus"},
        {"email": "soft@b.test", "source": "api"},
        {"subscriber_uuid": "nope", "source": "api", "type": "hard"},
        {"email": "nope", "source": "api", "type": "hard"},
        {"email": "soft@b.test", "source": "api", "type": "soft", "created_at": "yesterday"},
    ]
    for body in rejected:
        status, payload = app.call("POST", "/webhooks/bounce", body)
        assert status == 400 and set(payload) == {"message"}, (body, status)
    assert app.call("POST", "/webhooks/bounce", b"{x", ctype="application/json")[0] == 400

    bouncer.post(subscriber_uuid=subs["uu"]["uuid"], source="by-uuid", type="soft", meta=[1, 2])
    bouncer.post(email="none@b.test", type="soft", campaign_uuid=campaign_uuid)
    bouncer.post(email="ghost@b.test", source="api", type="hard")
    bouncer.post(subscriber_uuid="00000000-0000-0000-0000-000000000001", source="api", type="hard")
    bouncer.settle()
    assert bouncer.records() == [("uu@b.test", "soft", "by-uuid", [1, 2]), ("none@b.test", "soft", "", {})]
    assert app.q("SELECT b.campaign_id FROM bounces b JOIN subscribers s ON s.id = b.subscriber_id WHERE s.email IN ('uu@b.test', 'none@b.test') ORDER BY b.id") == [
        (None,), (campaign_id,)]
    listed = app.get(f"/api/bounces?campaign_id={campaign_id}")
    assert_json(listed["total"], 1)
    assert_json(listed["results"][0]["campaign"], {"id": campaign_id, "name": campaign_name})
    assert app.q("SELECT COUNT(*) FROM subscribers WHERE email = 'ghost@b.test'") == [(0,)]


def test_bounce_records_api(bouncing):
    app, subs, bouncer = bouncing
    app.configure(enable_bounces)
    bouncer.post(email="soft@b.test", source="api", type="soft", meta={"i": 0}, created_at="2024-03-01T00:00:00Z")
    bouncer.post(email="soft@b.test", source="api", type="soft", meta={"i": 1}, created_at="2024-03-02T00:00:00Z")
    bouncer.post(email="hard@b.test", source="mta", type="hard", created_at="2024-02-01T00:00:00Z")
    bouncer.post(email="uu@b.test", source="api", type="soft", created_at="2024-04-01T00:00:00Z")
    bouncer.settle()
    sentinel_ids = {r[0] for r in app.q("SELECT b.id FROM bounces b JOIN subscribers s ON s.id = b.subscriber_id WHERE s.email LIKE '%%@sentinel.test'")}
    ids = {email: [r[0] for r in app.q("SELECT b.id FROM bounces b JOIN subscribers s ON s.id = b.subscriber_id WHERE s.email = %s ORDER BY b.id", email)]
           for email in ("soft@b.test", "hard@b.test", "uu@b.test")}

    listing = app.get("/api/bounces?per_page=all")
    assert_json(listing["total"], 4 + len(sentinel_ids))
    record = next(r for r in listing["results"] if r["id"] == ids["hard@b.test"][0])
    assert_json({k: record[k] for k in ("id", "type", "source", "meta", "email", "subscriber_uuid", "subscriber_id", "subscriber_status", "campaign")},
                {"id": ids["hard@b.test"][0], "type": "hard", "source": "mta", "meta": {}, "email": "hard@b.test",
                 "subscriber_uuid": subs["hard"]["uuid"], "subscriber_id": subs["hard"]["id"], "subscriber_status": "blocklisted", "campaign": None})
    assert record["created_at"].startswith("2024-02-01T00:00:00")
    assert [r["email"] for r in app.get("/api/bounces?source=api&order_by=created_at&order=asc")["results"]] == ["soft@b.test", "soft@b.test", "uu@b.test"]
    assert [r["source"] for r in app.get("/api/bounces?source=mta")["results"]] == ["mta"]
    by_email = [r["email"] for r in app.get("/api/bounces?per_page=all&order_by=email&order=desc")["results"]]
    assert [e for e in by_email if e.endswith("@b.test")] == ["uu@b.test", "soft@b.test", "soft@b.test", "hard@b.test"]

    one = app.get(f"/api/bounces/{ids['uu@b.test'][0]}")
    assert (one["email"], one["type"], one["source"]) == ("uu@b.test", "soft", "api")
    assert app.call("GET", "/api/bounces/99999")[0] == 400
    of_sub = app.get(f"/api/subscribers/{subs['soft']['id']}/bounces")
    assert_json(sorted((r["meta"] for r in of_sub), key=lambda m: m["i"]), [{"i": 0}, {"i": 1}])
    assert {r["email"] for r in of_sub} == {"soft@b.test"}

    assert app.ok("DELETE", f"/api/bounces/{ids['uu@b.test'][0]}") is True
    assert app.q("SELECT COUNT(*) FROM bounces WHERE id = %s", ids["uu@b.test"][0]) == [(0,)]
    remaining = lambda: sorted(r[0] for r in app.q("SELECT id FROM bounces"))
    before = remaining()
    assert app.ok("DELETE", "/api/bounces/99999") is True
    assert app.call("DELETE", "/api/bounces")[0] == 400
    assert remaining() == before
    assert app.ok("DELETE", f"/api/subscribers/{subs['soft']['id']}/bounces") is True
    assert remaining() == sorted(set(before) - set(ids["soft@b.test"]))

    # Blocklisting every bounced subscriber also unsubscribes them.
    bouncer.post(email="none@b.test", source="api", type="soft")
    bouncer.settle()
    assert app.ok("PUT", "/api/bounces/blocklist") is True
    state = dict((r[0], (r[1], r[2])) for r in app.q(STATE_SQL))
    assert state["none@b.test"] == ("blocklisted", ["unsubscribed", "unsubscribed"])
    assert state["hard@b.test"] == ("blocklisted", ["unsubscribed", "unsubscribed"])
    assert state["soft@b.test"] == ("enabled", ["confirmed", "confirmed"])
    assert state["uu@b.test"] == ("enabled", ["confirmed", "confirmed"])
    assert app.ok("DELETE", "/api/bounces?all=true") is True
    assert app.q("SELECT COUNT(*) FROM bounces") == [(0,)]
    empty = app.get("/api/bounces")
    assert empty["results"] == []
    assert_json(empty["total"], 0)
