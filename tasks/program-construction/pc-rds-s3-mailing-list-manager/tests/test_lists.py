"""List operations: creation defaults, validation, updates, queries and deletion effects."""

import urllib.parse

from mailer import assert_id, assert_json

LIST_FIELDS = ("id", "uuid", "name", "type", "optin", "status", "tags", "description")


def row(app, list_id):
    return app.q("SELECT name, type::text, optin::text, status::text, COALESCE(tags, '{}'), description FROM lists WHERE id = %s", list_id)


def all_rows(app):
    return app.q("SELECT name, type::text, optin::text, status::text, COALESCE(tags, '{}'), description FROM lists ORDER BY id")


def names(app, **params):
    data = app.get("/api/lists?" + urllib.parse.urlencode({"per_page": "all", **params}, doseq=True))
    return [item["name"] for item in data["results"]]


def test_create_list_fields_defaults_and_tags(app):
    created = app.ok("POST", "/api/lists", {"name": "Weekly News", "type": "public", "optin": "double",
                                            "tags": ["News ", "Weekly", "a b"], "description": "Every Friday"})
    assert_id(created["id"])
    assert isinstance(created["uuid"], str) and len(created["uuid"]) == 36
    assert_json({k: created[k] for k in LIST_FIELDS[2:]},
                {"name": "Weekly News", "type": "public", "optin": "double", "status": "active", "tags": ["News", "Weekly", "a-b"], "description": "Every Friday"})
    assert row(app, created["id"]) == [("Weekly News", "public", "double", "active", ["News", "Weekly", "a-b"], "Every Friday")]
    assert app.q("SELECT uuid::text FROM lists WHERE id = %s", created["id"]) == [(created["uuid"],)]

    bare = app.ok("POST", "/api/lists", {"name": "Bare"})
    assert_json({k: bare[k] for k in LIST_FIELDS[2:]},
                {"name": "Bare", "type": "private", "optin": "single", "status": "active", "tags": [], "description": ""})
    temporary = app.ok("POST", "/api/lists", {"name": "Scratch", "type": "temporary", "status": "archived"})
    assert row(app, temporary["id"]) == [("Scratch", "temporary", "single", "archived", [], "")]
    # Names need not be unique.
    again = app.ok("POST", "/api/lists", {"name": "Bare"})
    assert again["id"] not in (bare["id"], created["id"], temporary["id"])
    assert app.get(f"/api/lists/{created['id']}")["uuid"] == created["uuid"]


def test_invalid_lists_are_rejected_without_rows(app):
    before = all_rows(app)
    cases = [
        ({"name": ""}, 400),
        ({"name": "x" * 2001}, 400),
        ({"name": "Secret", "type": "secret"}, 500),
        ({"name": "Triple", "optin": "triple"}, 500),
    ]
    for body, expected in cases:
        status, payload = app.call("POST", "/api/lists", body)
        assert status == expected, (body, status, payload)
        assert set(payload) == {"message"} and isinstance(payload["message"], str)
    status, _ = app.call("POST", "/api/lists", b"{not json")
    assert status == 400
    assert all_rows(app) == before


def test_get_and_update_list(app):
    created = app.create_list("Weekly News", "public", "double", tags=["news"], description="desc")
    list_id = created["id"]
    fetched = app.get(f"/api/lists/{list_id}")
    assert_json({k: fetched[k] for k in LIST_FIELDS},
                {"id": list_id, "uuid": created["uuid"], "name": "Weekly News", "type": "public", "optin": "double", "status": "active", "tags": ["news"], "description": "desc"})
    for path in ("/api/lists/99999", "/api/lists/abc"):
        status, payload = app.call("GET", path)
        assert status == 400 and set(payload) == {"message"}, (path, status, payload)

    # A name-only update keeps type/optin/status/description but replaces the tags.
    updated = app.ok("PUT", f"/api/lists/{list_id}", {"name": "Weekly Digest"})
    assert_json({k: updated[k] for k in LIST_FIELDS[2:]},
                {"name": "Weekly Digest", "type": "public", "optin": "double", "status": "active", "tags": [], "description": "desc"})
    assert row(app, list_id) == [("Weekly Digest", "public", "double", "active", [], "desc")]

    full = app.ok("PUT", f"/api/lists/{list_id}", {"name": "Digest", "type": "private", "optin": "single", "status": "archived", "tags": ["x y"], "description": ""})
    assert_json({k: full[k] for k in LIST_FIELDS[2:]},
                {"name": "Digest", "type": "private", "optin": "single", "status": "archived", "tags": ["x-y"], "description": "desc"})
    assert row(app, list_id) == [("Digest", "private", "single", "archived", ["x-y"], "desc")]

    before = all_rows(app)
    status, _ = app.call("PUT", "/api/lists/99999", {"name": "Ghost"})
    assert status == 400
    status, _ = app.call("PUT", f"/api/lists/{list_id}", {"name": ""})
    assert status == 400
    assert all_rows(app) == before


def test_list_queries_filter_and_order(app):
    app.create_list("Morning Digest", "public", "single", tags=["daily", "news"])
    app.create_list("Evening Digest", "private", "double", tags=["daily"])
    app.create_list("Archive", "public", "double", status="archived", tags=["news"])
    app.create_list("Scratch", "temporary", "single")

    assert sorted(names(app, query="DIGEST")) == ["Evening Digest", "Morning Digest"]
    assert sorted(names(app, query="morn")) == ["Morning Digest"]
    assert sorted(names(app, type="public")) == ["Archive", "Morning Digest", "Opt-in list"]
    assert sorted(names(app, type="temporary")) == ["Scratch"]
    assert sorted(names(app, optin="double")) == ["Archive", "Evening Digest", "Opt-in list"]
    assert sorted(names(app, status="archived")) == ["Archive"]
    assert sorted(names(app, tag="daily")) == ["Evening Digest", "Morning Digest"]
    assert sorted(names(app, tag=["daily", "news"])) == ["Morning Digest"]
    assert sorted(names(app, type="public", tag="news")) == ["Archive", "Morning Digest"]

    every = ["Archive", "Default list", "Evening Digest", "Morning Digest", "Opt-in list", "Scratch"]
    assert names(app, order_by="name", order="asc") == every
    assert names(app, order_by="name", order="desc") == every[::-1]
    page = app.get("/api/lists?order_by=name&order=asc&per_page=2&page=2")
    assert [item["name"] for item in page["results"]] == every[2:4]
    assert_json({k: page[k] for k in ("total", "page", "per_page")}, {"total": 6, "page": 2, "per_page": 2})
    assert_json(app.get("/api/lists?per_page=all")["total"], 6)


def test_delete_list_removes_its_memberships_only(app):
    keep = app.create_list("Keep")
    drop = app.create_list("Drop")
    first = app.create_subscriber("first@lists.test", lists=[keep["id"], drop["id"]], preconfirm_subscriptions=True)
    second = app.create_subscriber("second@lists.test", lists=[drop["id"]])
    assert app.ok("DELETE", f"/api/lists/{drop['id']}") is True
    assert app.q("SELECT id FROM lists WHERE id = %s", drop["id"]) == []
    assert app.q("SELECT subscriber_id, list_id, status::text FROM subscriber_lists WHERE subscriber_id IN (%s, %s) ORDER BY 1, 2",
                 first["id"], second["id"]) == [(first["id"], keep["id"], "confirmed")]
    assert sorted(r[0] for r in app.q("SELECT email FROM subscribers WHERE email LIKE '%%@lists.test'")) == ["first@lists.test", "second@lists.test"]
    status, _ = app.call("GET", f"/api/lists/{drop['id']}")
    assert status == 400
    # Deleting an unknown list succeeds without changing anything.
    before = all_rows(app)
    assert app.ok("DELETE", "/api/lists/99999") is True
    assert all_rows(app) == before


def test_bulk_list_deletion(app):
    a = app.create_list("Alpha Weekly")
    b = app.create_list("Beta Weekly")
    c = app.create_list("Gamma Monthly")
    before = all_rows(app)
    for path in ("/api/lists", "/api/lists?id=abc"):
        status, payload = app.call("DELETE", path)
        assert status == 400 and set(payload) == {"message"}, (path, status)
        assert all_rows(app) == before

    assert app.ok("DELETE", f"/api/lists?id={a['id']}&id={c['id']}") is True
    assert [r[0] for r in all_rows(app)] == ["Default list", "Opt-in list", "Beta Weekly"]
    assert app.ok("DELETE", "/api/lists?query=weekly") is True
    assert [r[0] for r in all_rows(app)] == ["Default list", "Opt-in list"]
    assert b["id"] not in [r[0] for r in app.q("SELECT id FROM lists")]
