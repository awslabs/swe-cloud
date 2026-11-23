"""Subscriber operations: creation, validation, full and partial updates, blocklisting and deletion."""

from mailer import assert_id, assert_json

SUB_FIELDS = ("id", "uuid", "email", "name", "attribs", "status")


def memberships(app, subscriber_id):
    return app.q("SELECT l.name, sl.status::text FROM subscriber_lists sl JOIN lists l ON l.id = sl.list_id "
                 "WHERE sl.subscriber_id = %s ORDER BY 1", subscriber_id)


# A JSON null attribs value is equivalent to an empty object.
ATTRIBS = "COALESCE(NULLIF(attribs, 'null'::jsonb), '{}'::jsonb)"


def subscriber_row(app, subscriber_id):
    return app.q(f"SELECT email, name, status::text, {ATTRIBS} FROM subscribers WHERE id = %s", subscriber_id)


def all_subscribers(app):
    return app.q(f"SELECT id, email, name, status::text, {ATTRIBS} FROM subscribers ORDER BY id")


def all_memberships(app):
    return app.q("SELECT subscriber_id, list_id, status::text FROM subscriber_lists ORDER BY 1, 2")


def api_lists(subscriber):
    return sorted((item["name"], item["subscription_status"]) for item in subscriber["lists"])


def make_lists(app):
    return {
        "single": app.create_list("Single", "private", "single")["id"],
        "double": app.create_list("Double", "public", "double")["id"],
        "other": app.create_list("Other", "public", "single")["id"],
    }


def test_create_normalizes_and_derives_fields(app):
    lists = make_lists(app)
    attribs = {"n": 1, "f": 1.5, "b": True, "off": False, "nested": {"x": [1, "y", None]}}
    created = app.create_subscriber("  Mixed.Case@Example.COM ", lists=[lists["single"], lists["double"]], attribs=attribs)
    assert_json({k: created[k] for k in SUB_FIELDS[2:]},
                {"email": "mixed.case@example.com", "name": "Mixed Case", "attribs": attribs, "status": "enabled"})
    assert_id(created["id"])
    assert len(created["uuid"]) == 36
    assert app.q("SELECT uuid::text FROM subscribers WHERE id = %s", created["id"]) == [(created["uuid"],)]
    assert subscriber_row(app, created["id"]) == [("mixed.case@example.com", "Mixed Case", "enabled", attribs)]
    stored = app.q("SELECT jsonb_typeof(attribs->'b'), jsonb_typeof(attribs->'n'), (attribs->>'f')::numeric FROM subscribers WHERE id = %s", created["id"])
    assert stored == [("boolean", "number", 1.5)]
    # Without preconfirmation every new membership is unconfirmed, single or double opt-in alike.
    assert memberships(app, created["id"]) == [("Double", "unconfirmed"), ("Single", "unconfirmed")]
    assert api_lists(created) == [("Double", "unconfirmed"), ("Single", "unconfirmed")]

    named = app.create_subscriber("first.second-third@x.test", name="   ")
    assert named["name"] == "First Second-Third"
    explicit = app.create_subscriber("plain@x.test", name="  Given Name ")
    assert subscriber_row(app, explicit["id"]) == [("plain@x.test", "Given Name", "enabled", {})]
    disabled = app.create_subscriber("off@x.test", status="disabled")
    assert disabled["status"] == "disabled" and disabled["lists"] == []


def test_create_membership_statuses(app):
    lists = make_lists(app)
    confirmed = app.create_subscriber("pre@x.test", lists=[lists["single"], lists["double"], 99999], preconfirm_subscriptions=True)
    assert memberships(app, confirmed["id"]) == [("Double", "confirmed"), ("Single", "confirmed")]
    assert api_lists(confirmed) == [("Double", "confirmed"), ("Single", "confirmed")]
    blocked = app.create_subscriber("blk@x.test", status="blocklisted", lists=[lists["single"]], preconfirm_subscriptions=True)
    assert blocked["status"] == "blocklisted"
    assert memberships(app, blocked["id"]) == [("Single", "unsubscribed")]
    assert api_lists(app.get(f"/api/subscribers/{blocked['id']}")) == [("Single", "unsubscribed")]


def test_create_rejections_leave_state_unchanged(app):
    lists = make_lists(app)
    app.create_subscriber("taken@x.test", lists=[lists["single"]])
    before = (all_subscribers(app), all_memberships(app))
    cases = [
        ({"email": "TAKEN@x.test", "lists": [lists["other"]]}, 409),
        ({"email": "not-an-email"}, 400),
        ({"name": "No Email"}, 400),
        ({"email": "Joe <joe@x.test>"}, 400),
        ({"email": "arr@x.test", "attribs": [1, 2]}, 400),
        ({"email": "weird@x.test", "status": "weird"}, 500),
    ]
    for body, expected in cases:
        status, payload = app.call("POST", "/api/subscribers", body)
        assert status == expected, (body, status, payload)
        assert set(payload) == {"message"}
    assert (all_subscribers(app), all_memberships(app)) == before


def test_get_subscriber(app):
    lists = make_lists(app)
    created = app.create_subscriber("get@x.test", name="Getter", attribs={"k": [1, 2]}, lists=[lists["double"]], preconfirm_subscriptions=True)
    fetched = app.get(f"/api/subscribers/{created['id']}")
    assert_json({k: fetched[k] for k in SUB_FIELDS},
                {"id": created["id"], "uuid": created["uuid"], "email": "get@x.test", "name": "Getter", "attribs": {"k": [1, 2]}, "status": "enabled"})
    assert len(fetched["lists"]) == 1
    entry = fetched["lists"][0]
    assert_json({k: entry[k] for k in ("id", "name", "type", "optin", "subscription_status")},
                {"id": lists["double"], "name": "Double", "type": "public", "optin": "double", "subscription_status": "confirmed"})
    for path in ("/api/subscribers/99999", "/api/subscribers/0", "/api/subscribers/abc"):
        status, payload = app.call("GET", path)
        assert status == 400 and set(payload) == {"message"}, (path, status)


def test_put_replaces_profile_and_memberships(app):
    lists = make_lists(app)
    sub = app.create_subscriber("put@x.test", name="Before", attribs={"old": 1}, lists=[lists["single"], lists["double"]])
    sid = sub["id"]
    app.ok("PUT", "/api/subscribers/lists", {"ids": [sid], "action": "unsubscribe", "target_list_ids": [lists["single"]]})

    out = app.ok("PUT", f"/api/subscribers/{sid}", {"email": "put@x.test", "name": "Renamed", "status": "enabled",
                                                    "lists": [lists["single"], lists["other"]], "attribs": {"k": "v"}})
    # Unlisted memberships are dropped, an unsubscribed one stays unsubscribed, new ones are unconfirmed.
    assert memberships(app, sid) == [("Other", "unconfirmed"), ("Single", "unsubscribed")]
    assert api_lists(out) == [("Other", "unconfirmed"), ("Single", "unsubscribed")]
    assert subscriber_row(app, sid) == [("put@x.test", "Renamed", "enabled", {"k": "v"})]

    # Preconfirmation upgrades unconfirmed memberships but not unsubscribed ones; omitted attribs are cleared.
    app.ok("PUT", f"/api/subscribers/{sid}", {"email": "put@x.test", "name": "Renamed",
                                              "lists": [lists["single"], lists["other"], lists["double"]], "preconfirm_subscriptions": True})
    assert memberships(app, sid) == [("Double", "confirmed"), ("Other", "confirmed"), ("Single", "unsubscribed")]
    assert subscriber_row(app, sid) == [("put@x.test", "Renamed", "enabled", {})]

    # Omitting lists removes every membership; the e-mail may change and is normalized.
    app.ok("PUT", f"/api/subscribers/{sid}", {"email": "Moved@X.test", "name": "Renamed"})
    assert memberships(app, sid) == []
    assert subscriber_row(app, sid) == [("moved@x.test", "Renamed", "enabled", {})]


def test_put_rejections(app):
    lists = make_lists(app)
    first = app.create_subscriber("one@x.test", lists=[lists["single"]])
    app.create_subscriber("two@x.test")
    before = (all_subscribers(app), all_memberships(app))
    cases = [
        (first["id"], {"email": "zz", "name": "Q"}, 400),
        (first["id"], {"email": "two@x.test", "name": "Q", "lists": [lists["single"]]}, 500),
        (99999, {"email": "ghost@x.test", "name": "Q"}, 400),
    ]
    for sid, body, expected in cases:
        status, payload = app.call("PUT", f"/api/subscribers/{sid}", body)
        assert status == expected, (sid, body, status, payload)
        assert (all_subscribers(app), all_memberships(app)) == before


def test_patch_updates_only_given_fields(app):
    lists = make_lists(app)
    sub = app.create_subscriber("patch@x.test", name="Orig", attribs={"a": 1, "n": {"x": 1, "y": 2}}, lists=[lists["single"], lists["double"]], preconfirm_subscriptions=True)
    sid = sub["id"]
    out = app.ok("PATCH", f"/api/subscribers/{sid}", {"name": "Patched"})
    assert api_lists(out) == [("Double", "confirmed"), ("Single", "confirmed")]
    assert subscriber_row(app, sid) == [("patch@x.test", "Patched", "enabled", {"a": 1, "n": {"x": 1, "y": 2}})]
    assert memberships(app, sid) == [("Double", "confirmed"), ("Single", "confirmed")]

    # Patched attributes are merged into the existing top-level keys.
    out = app.ok("PATCH", f"/api/subscribers/{sid}", {"attribs": {"z": True, "n": {"x": 5}}})
    merged = {"a": 1, "n": {"x": 5}, "z": True}
    assert_json(out["attribs"], merged)
    assert subscriber_row(app, sid) == [("patch@x.test", "Patched", "enabled", merged)]
    assert memberships(app, sid) == [("Double", "confirmed"), ("Single", "confirmed")]

    app.ok("PATCH", f"/api/subscribers/{sid}", {"lists": [lists["single"], lists["other"]]})
    assert memberships(app, sid) == [("Other", "unconfirmed"), ("Single", "confirmed")]
    app.ok("PATCH", f"/api/subscribers/{sid}", {"lists": []})
    assert memberships(app, sid) == []

    # Blocklisting through PATCH unsubscribes; re-enabling does not resubscribe.
    app.ok("PATCH", f"/api/subscribers/{sid}", {"status": "blocklisted", "lists": [lists["single"]], "preconfirm_subscriptions": True})
    assert memberships(app, sid) == [("Single", "unsubscribed")]
    assert subscriber_row(app, sid)[0][2] == "blocklisted"
    app.ok("PATCH", f"/api/subscribers/{sid}", {"status": "enabled"})
    assert memberships(app, sid) == [("Single", "unsubscribed")]
    assert subscriber_row(app, sid) == [("patch@x.test", "Patched", "enabled", merged)]

    before = (all_subscribers(app), all_memberships(app))
    for target, body in ((99999, {"name": "x"}), (sid, {"email": "bad"})):
        status, _ = app.call("PATCH", f"/api/subscribers/{target}", body)
        assert status == 400, (target, body, status)
        assert (all_subscribers(app), all_memberships(app)) == before


def test_blocklist_single_and_bulk(app):
    lists = make_lists(app)
    a = app.create_subscriber("a@blk.test", lists=[lists["single"], lists["other"]], preconfirm_subscriptions=True)
    b = app.create_subscriber("b@blk.test", lists=[lists["single"]])
    c = app.create_subscriber("c@blk.test", lists=[lists["double"]])
    bystander = app.create_subscriber("d@blk.test", lists=[lists["single"]], preconfirm_subscriptions=True)

    assert app.ok("PUT", f"/api/subscribers/{a['id']}/blocklist") is True
    assert subscriber_row(app, a["id"])[0][2] == "blocklisted"
    assert memberships(app, a["id"]) == [("Other", "unsubscribed"), ("Single", "unsubscribed")]
    assert memberships(app, b["id"]) == [("Single", "unconfirmed")]

    assert app.ok("PUT", "/api/subscribers/blocklist", {"ids": [b["id"], c["id"]]}) is True
    for sub, expected in ((b, [("Single", "unsubscribed")]), (c, [("Double", "unsubscribed")])):
        assert subscriber_row(app, sub["id"])[0][2] == "blocklisted"
        assert memberships(app, sub["id"]) == expected
    assert subscriber_row(app, bystander["id"])[0][2] == "enabled"
    assert memberships(app, bystander["id"]) == [("Single", "confirmed")]

    before = (all_subscribers(app), all_memberships(app))
    status, _ = app.call("PUT", "/api/subscribers/blocklist", {"ids": []})
    assert status == 400
    assert app.ok("PUT", "/api/subscribers/99999/blocklist") is True
    assert (all_subscribers(app), all_memberships(app)) == before


def test_delete_single_and_bulk(app):
    lists = make_lists(app)
    subs = [app.create_subscriber(f"del{i}@x.test", lists=[lists["single"]]) for i in range(4)]
    assert app.ok("DELETE", f"/api/subscribers/{subs[0]['id']}") is True
    assert app.q("SELECT COUNT(*) FROM subscriber_lists WHERE subscriber_id = %s", subs[0]["id"]) == [(0,)]
    status, _ = app.call("GET", f"/api/subscribers/{subs[0]['id']}")
    assert status == 400
    assert app.ok("DELETE", f"/api/subscribers?id={subs[1]['id']}&id={subs[2]['id']}") is True
    remaining = [r[0] for r in app.q("SELECT email FROM subscribers WHERE email LIKE 'del%%' ORDER BY 1")]
    assert remaining == ["del3@x.test"]
    before = (all_subscribers(app), all_memberships(app))
    assert app.ok("DELETE", "/api/subscribers/99999") is True
    for path in ("/api/subscribers", "/api/subscribers?id=abc"):
        status, _ = app.call("DELETE", path)
        assert status == 400, path
    assert (all_subscribers(app), all_memberships(app)) == before
    assert [r[0] for r in app.q("SELECT name FROM lists ORDER BY id")] == ["Default list", "Opt-in list", "Single", "Double", "Other"]
