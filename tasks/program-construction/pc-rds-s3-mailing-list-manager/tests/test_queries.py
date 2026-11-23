"""Segmentation: SQL-expression and search queries, list filters, and bulk actions by IDs or by query."""

import urllib.parse

import pytest

from mailer import assert_json

PEOPLE = [
    # email, name, attribs, list keys, preconfirm
    ("ann@q.test", "Ann", {"city": "Oslo", "score": 9, "tags": ["vip"]}, ["A"], True),
    ("ben@q.test", "Ben", {"city": "Rome", "score": 2}, ["A", "B"], False),
    ("cat@q.test", "Cat", {"city": "Oslo", "score": 5}, ["B"], True),
    ("dan@q.example", "Dan Oslo", {"city": "Lima", "score": 7, "tags": ["new"]}, [], False),
]


@pytest.fixture
def segment(app):
    lists = {key: app.create_list("Q" + key)["id"] for key in "ABC"}
    ids = {}
    for email, name, attribs, keys, preconfirm in PEOPLE:
        ids[email] = app.create_subscriber(email, name=name, attribs=attribs, lists=[lists[k] for k in keys],
                                           preconfirm_subscriptions=preconfirm)["id"]
    return app, lists, ids


def emails(app, **params):
    data = app.query_subscribers(**params)
    found = sorted(item["email"] for item in data["results"])
    assert_json(data["total"], len(found))
    return found


def memberships(app):
    return sorted(app.q("SELECT s.email, l.name, sl.status::text FROM subscriber_lists sl JOIN subscribers s ON s.id = sl.subscriber_id "
                        "JOIN lists l ON l.id = sl.list_id"))


def statuses(app):
    return sorted(app.q("SELECT email, status::text FROM subscribers"))


INITIAL = sorted([
    ("ann@q.test", "QA", "confirmed"), ("ben@q.test", "QA", "unconfirmed"), ("ben@q.test", "QB", "unconfirmed"),
    ("cat@q.test", "QB", "confirmed"), ("anon@example.com", "Opt-in list", "unconfirmed"), ("john@example.com", "Default list", "unconfirmed"),
])


def test_sql_expression_queries(segment):
    app, lists, ids = segment
    assert emails(app, query="(subscribers.attribs->>'score')::INT > 5") == ["ann@q.test", "dan@q.example"]
    assert emails(app, query="subscribers.attribs->>'city' = 'Oslo'") == ["ann@q.test", "cat@q.test"]
    assert emails(app, query="subscribers.attribs->'tags' ? 'vip'") == ["ann@q.test"]
    assert emails(app, query="email LIKE '%q.test'") == ["ann@q.test", "ben@q.test", "cat@q.test"]
    assert emails(app, query="subscribers.email = 'ben@q.test';") == ["ben@q.test"]
    assert emails(app, query="subscribers.attribs->>'good' = 'true'") == ["anon@example.com", "john@example.com"]
    for bad in ("subscribers.nope = 1", "((", "1=1; DROP TABLE lists"):
        status, payload = app.call("GET", "/api/subscribers?" + urllib.parse.urlencode({"query": bad}))
        assert status == 400 and set(payload) == {"message"}, (bad, status)
    assert app.q("SELECT COUNT(*) FROM lists") == [(5,)]


def test_search_and_list_filters(segment):
    app, lists, ids = segment
    assert emails(app, search="oslo") == ["dan@q.example"]
    assert emails(app, search="q.example") == ["dan@q.example"]
    assert emails(app, search="BEN") == ["ben@q.test"]
    assert emails(app, list_id=lists["A"]) == ["ann@q.test", "ben@q.test"]
    assert emails(app, list_id=lists["B"], subscription_status="confirmed") == ["cat@q.test"]
    assert emails(app, list_id=lists["B"], subscription_status="unconfirmed") == ["ben@q.test"]
    assert emails(app, list_id=lists["C"]) == []
    assert emails(app, list_id=lists["A"], query="(subscribers.attribs->>'score')::INT > 5") == ["ann@q.test"]
    assert emails(app, query="subscribers.email LIKE '%q.test'", search="ca") == ["cat@q.test"]
    # Without a list filter the subscription status filter has no effect.
    assert len(emails(app, subscription_status="confirmed")) == 6


def test_ordering_and_pagination(segment):
    app, _, _ = segment
    data = app.query_subscribers(order_by="name", order="desc")
    assert [item["name"] for item in data["results"]] == ["John Doe", "Dan Oslo", "Cat", "Ben", "Anon Doe", "Ann"]
    data = app.query_subscribers(order_by="email", order="asc")
    assert [item["email"] for item in data["results"]] == sorted(r[0] for r in app.q("SELECT email FROM subscribers"))
    page = app.get("/api/subscribers?order_by=email&order=asc&per_page=2&page=2")
    assert [item["email"] for item in page["results"]] == ["ben@q.test", "cat@q.test"]
    assert_json([page["total"], page["page"], page["per_page"]], [6, 2, 2])
    default = app.get("/api/subscribers")
    assert_json([default["total"], default["page"], default["per_page"], len(default["results"])], [6, 1, 20, 6])
    result = next(item for item in app.query_subscribers(list_id=app.q("SELECT id FROM lists WHERE name='QB'")[0][0])["results"] if item["email"] == "cat@q.test")
    assert result["attribs"] == {"city": "Oslo", "score": 5} and result["status"] == "enabled"
    assert [(entry["name"], entry["subscription_status"]) for entry in result["lists"]] == [("QB", "confirmed")]


def test_bulk_list_actions_by_ids(segment):
    app, lists, ids = segment
    assert memberships(app) == INITIAL
    ann, ben, cat, dan = (ids[p[0]] for p in PEOPLE)

    app.ok("PUT", "/api/subscribers/lists", {"ids": [ann, dan], "action": "add", "target_list_ids": [lists["B"]]})
    state = sorted(INITIAL + [("ann@q.test", "QB", "unconfirmed"), ("dan@q.example", "QB", "unconfirmed")])
    assert memberships(app) == state

    # An explicit status also overrides existing memberships.
    app.ok("PUT", "/api/subscribers/lists", {"ids": [ann, ben], "action": "add", "target_list_ids": [lists["B"], lists["C"]], "status": "confirmed"})
    state = sorted([m for m in state if not (m[0] in ("ann@q.test", "ben@q.test") and m[1] == "QB")] + [
        ("ann@q.test", "QB", "confirmed"), ("ben@q.test", "QB", "confirmed"), ("ann@q.test", "QC", "confirmed"), ("ben@q.test", "QC", "confirmed")])
    assert memberships(app) == state

    app.ok("PUT", "/api/subscribers/lists", {"ids": [ben], "action": "remove", "target_list_ids": [lists["A"], lists["C"]]})
    state = [m for m in state if not (m[0] == "ben@q.test" and m[1] in ("QA", "QC"))]
    assert memberships(app) == state

    app.ok("PUT", "/api/subscribers/lists", {"ids": [cat, ann], "action": "unsubscribe", "target_list_ids": [lists["B"]]})
    state = sorted([m for m in state if not (m[0] in ("cat@q.test", "ann@q.test") and m[1] == "QB")] + [
        ("ann@q.test", "QB", "unsubscribed"), ("cat@q.test", "QB", "unsubscribed")])
    assert memberships(app) == state

    # Adding without a status keeps an existing membership's status.
    app.ok("PUT", "/api/subscribers/lists", {"ids": [cat], "action": "add", "target_list_ids": [lists["B"]]})
    assert memberships(app) == state

    rejected = [
        ({"ids": [cat], "action": "frob", "target_list_ids": [lists["B"]]}, 400),
        ({"ids": [], "action": "add", "target_list_ids": [lists["B"]]}, 400),
        ({"ids": [cat], "action": "add", "target_list_ids": []}, 400),
        ({"ids": [cat], "action": "add", "target_list_ids": [lists["C"]], "status": "weird"}, 500),
        ({"ids": [cat, ben], "action": "add", "target_list_ids": [lists["C"], 99999]}, 500),
    ]
    for body, expected in rejected:
        status, payload = app.call("PUT", "/api/subscribers/lists", body)
        assert status == expected, (body, status, payload)
        assert memberships(app) == state


def test_bulk_list_actions_by_query(segment):
    app, lists, ids = segment
    oslo = "subscribers.attribs->>'city' = 'Oslo'"
    app.ok("PUT", "/api/subscribers/query/lists", {"query": oslo, "action": "add", "target_list_ids": [lists["C"]], "status": "unsubscribed"})
    state = sorted(INITIAL + [("ann@q.test", "QC", "unsubscribed"), ("cat@q.test", "QC", "unsubscribed")])
    assert memberships(app) == state

    # Adding by query never touches existing memberships.
    app.ok("PUT", "/api/subscribers/query/lists", {"query": "subscribers.email = 'ann@q.test'", "action": "add",
                                                   "target_list_ids": [lists["A"], lists["C"]], "status": "confirmed"})
    assert memberships(app) == state
    app.ok("PUT", "/api/subscribers/query/lists", {"search": "dan", "action": "add", "target_list_ids": [lists["C"]], "status": "confirmed"})
    state = sorted(state + [("dan@q.example", "QC", "confirmed")])
    assert memberships(app) == state

    # list_ids/subscription_status pick the source subscribers.
    app.ok("PUT", "/api/subscribers/query/lists", {"query": "", "list_ids": [lists["B"]], "subscription_status": "unconfirmed",
                                                   "action": "add", "target_list_ids": [lists["C"]], "status": "confirmed"})
    state = sorted(state + [("ben@q.test", "QC", "confirmed")])
    assert memberships(app) == state
    app.ok("PUT", "/api/subscribers/query/lists", {"query": "", "list_ids": [lists["A"]], "action": "unsubscribe", "target_list_ids": [lists["C"]]})
    state = sorted([m for m in state if m[:2] not in (("ben@q.test", "QC"),)] + [("ben@q.test", "QC", "unsubscribed")])
    assert memberships(app) == state

    # An empty query selects everyone; so does all=true.
    app.ok("PUT", "/api/subscribers/query/lists", {"query": "", "action": "remove", "target_list_ids": [lists["A"]]})
    state = [m for m in state if m[1] != "QA"]
    assert memberships(app) == state
    app.ok("PUT", "/api/subscribers/query/lists", {"all": True, "action": "remove", "target_list_ids": [lists["C"]]})
    state = [m for m in state if m[1] != "QC"]
    assert memberships(app) == state

    for body, expected in (({"query": "nope(", "action": "remove", "target_list_ids": [lists["B"]]}, 500),
                           ({"query": oslo, "action": "frob", "target_list_ids": [lists["B"]]}, 400),
                           ({"query": oslo, "action": "remove", "target_list_ids": []}, 400)):
        status, _ = app.call("PUT", "/api/subscribers/query/lists", body)
        assert status == expected, (body, status)
        assert memberships(app) == state


def test_blocklist_by_query(segment):
    app, lists, ids = segment
    before_people = statuses(app)
    for body in ({"query": ""}, {"query": "  ", "list_ids": [lists["B"]], "subscription_status": "unconfirmed"}):
        status, payload = app.call("PUT", "/api/subscribers/query/blocklist", body)
        assert status == 400, (body, status)
    status, _ = app.call("PUT", "/api/subscribers/query/blocklist", {"query": "zz("})
    assert status == 500
    assert statuses(app) == before_people and memberships(app) == INITIAL

    app.ok("PUT", "/api/subscribers/query/blocklist", {"query": "(subscribers.attribs->>'score')::INT >= 7"})
    assert statuses(app) == sorted([("ann@q.test", "blocklisted"), ("ben@q.test", "enabled"), ("cat@q.test", "enabled"),
                                    ("dan@q.example", "blocklisted"), ("anon@example.com", "enabled"), ("john@example.com", "enabled")])
    assert memberships(app) == sorted([m for m in INITIAL if m[0] != "ann@q.test"] + [("ann@q.test", "QA", "unsubscribed")])

    app.ok("PUT", "/api/subscribers/query/blocklist", {"search": "ben"})
    app.ok("PUT", "/api/subscribers/query/blocklist", {"all": True, "list_ids": [lists["B"]], "subscription_status": "confirmed"})
    assert dict(statuses(app)) == {"ann@q.test": "blocklisted", "ben@q.test": "blocklisted", "cat@q.test": "blocklisted",
                                   "dan@q.example": "blocklisted", "anon@example.com": "enabled", "john@example.com": "enabled"}
    assert memberships(app) == sorted([
        ("ann@q.test", "QA", "unsubscribed"), ("ben@q.test", "QA", "unsubscribed"), ("ben@q.test", "QB", "unsubscribed"),
        ("cat@q.test", "QB", "unsubscribed"), ("anon@example.com", "Opt-in list", "unconfirmed"), ("john@example.com", "Default list", "unconfirmed")])


def test_delete_by_query(segment):
    app, lists, ids = segment
    for body, expected in (({"query": ""}, 400), ({"search": " "}, 400), ({"query": "zzz("}, 500)):
        status, _ = app.call("POST", "/api/subscribers/query/delete", body)
        assert status == expected, (body, status)
    assert len(statuses(app)) == 6

    app.ok("POST", "/api/subscribers/query/delete", {"query": "subscribers.email LIKE '%q.example'"})
    assert [s[0] for s in statuses(app)] == ["ann@q.test", "anon@example.com", "ben@q.test", "cat@q.test", "john@example.com"]
    app.ok("POST", "/api/subscribers/query/delete", {"all": True, "list_ids": [lists["B"]]})
    assert [s[0] for s in statuses(app)] == ["ann@q.test", "anon@example.com", "john@example.com"]
    assert memberships(app) == sorted([("ann@q.test", "QA", "confirmed"), ("anon@example.com", "Opt-in list", "unconfirmed"),
                                       ("john@example.com", "Default list", "unconfirmed")])
    app.ok("POST", "/api/subscribers/query/delete", {"search": "ann"})
    assert [s[0] for s in statuses(app)] == ["anon@example.com", "john@example.com"]
