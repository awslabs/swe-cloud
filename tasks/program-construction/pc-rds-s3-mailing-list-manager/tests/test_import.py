"""CSV/ZIP subscriber import: modes, overwrite switches, delimiters, header handling, validation and status reporting."""

import io
import zipfile
from pathlib import Path

import pytest

from mailer import IMPORT_FIELDS, assert_import, assert_json, multipart

FIXTURES = Path(__file__).parent / "fixtures"
# Absent or invalid attributes are stored as JSON null; that is equivalent to an empty object.
PEOPLE_SQL = ("SELECT s.email, s.name, s.status::text, COALESCE(NULLIF(s.attribs, 'null'::jsonb), '{}'::jsonb), l.name, sl.status::text "
              "FROM subscribers s LEFT JOIN subscriber_lists sl ON sl.subscriber_id = s.id LEFT JOIN lists l ON l.id = sl.list_id "
              "WHERE s.email LIKE %s ORDER BY 1, 5")

CSV_MIXED = (
    "email,name,attributes,extra\n"
    'New.One@IMP.test,New One,"{""city"": ""Oslo"", ""n"": 2}",zzz\n'
    'old@imp.test,Old Renamed,"{""city"": ""Rome""}",q\n'
    "noname@imp.test,,,\n"
    "bad-email,Bad,,\n"
    "badjson@imp.test,BJ,{notjson,\n"
    "short@imp.test\n"
    "\n"
)


def people(app, pattern="%@imp.test"):
    return app.q(PEOPLE_SQL, pattern)


@pytest.fixture
def imp(app):
    lists = {name: app.create_list(name, "private", optin)["id"] for name, optin in (("I1", "single"), ("I2", "double"), ("I3", "single"))}
    old = app.create_subscriber("old@imp.test", name="Old", attribs={"keep": 1}, lists=[lists["I1"], lists["I3"]], preconfirm_subscriptions=True)
    app.ok("PUT", "/api/subscribers/lists", {"ids": [old["id"]], "action": "unsubscribe", "target_list_ids": [lists["I3"]]})
    return app, lists


def test_status_before_and_after_import(app):
    assert_import(app.get("/api/import/subscribers"), {"name": "", "total": 0, "imported": 0, "status": "none"})
    assert isinstance(app.get("/api/import/subscribers/logs"), str)
    list_id = app.create_list("Target")["id"]
    status, payload = app.start_import("email,name\nsolo@imp.test,Solo\n", {"mode": "subscribe", "delim": ",", "lists": [list_id]}, filename="first.csv")
    assert status == 200 and set(payload) == {"data"}
    assert set(IMPORT_FIELDS) <= set(payload["data"]) and payload["data"]["name"] == "first.csv"
    assert payload["data"]["status"] in ("importing", "finished")
    assert_import(app.wait_import(), {"name": "first.csv", "total": 1, "imported": 1, "status": "finished"})
    logs = app.get("/api/import/subscribers/logs")
    assert isinstance(logs, str)
    # Clearing a finished import resets the reported state.
    assert_import(app.ok("DELETE", "/api/import/subscribers"), {"name": "", "total": 0, "imported": 0, "status": "none"})
    assert_import(app.get("/api/import/subscribers"), {"name": "", "total": 0, "imported": 0, "status": "none"})
    assert isinstance(app.get("/api/import/subscribers/logs"), str)
    assert people(app) == [("solo@imp.test", "Solo", "enabled", {}, "Target", "unconfirmed")]


def test_subscribe_without_overwrite(imp):
    app, lists = imp
    final = app.run_import(CSV_MIXED, {"mode": "subscribe", "delim": ",", "lists": [lists["I1"], lists["I2"]], "overwrite": False})
    # total counts the file's lines after the header; imported counts accepted rows.
    assert_import(final, {"name": "subscribers.csv", "total": 7, "imported": 4, "status": "finished"})
    assert people(app) == [
        ("badjson@imp.test", "BJ", "enabled", {}, "I1", "unconfirmed"),
        ("badjson@imp.test", "BJ", "enabled", {}, "I2", "unconfirmed"),
        ("new.one@imp.test", "New One", "enabled", {"city": "Oslo", "n": 2}, "I1", "unconfirmed"),
        ("new.one@imp.test", "New One", "enabled", {"city": "Oslo", "n": 2}, "I2", "unconfirmed"),
        ("noname@imp.test", "Noname", "enabled", {}, "I1", "unconfirmed"),
        ("noname@imp.test", "Noname", "enabled", {}, "I2", "unconfirmed"),
        ("old@imp.test", "Old", "enabled", {"keep": 1}, "I1", "confirmed"),
        ("old@imp.test", "Old", "enabled", {"keep": 1}, "I2", "unconfirmed"),
        ("old@imp.test", "Old", "enabled", {"keep": 1}, "I3", "unsubscribed"),
    ]
    assert app.q("SELECT COUNT(*) FROM subscribers WHERE email IN ('bad-email', 'short@imp.test')") == [(0,)]
    noname = app.q("SELECT id FROM subscribers WHERE email = 'noname@imp.test'")[0][0]
    assert app.get(f"/api/subscribers/{noname}")["attribs"] == {}


def test_subscribe_with_overwrite(imp):
    app, lists = imp
    final = app.run_import(CSV_MIXED, {"mode": "subscribe", "delim": ",", "lists": [lists["I1"], lists["I3"]], "overwrite": True,
                                       "subscription_status": "confirmed"})
    assert_import(final, {"name": "subscribers.csv", "total": 7, "imported": 4, "status": "finished"})
    assert people(app, "old@%") == [
        ("old@imp.test", "Old Renamed", "enabled", {"city": "Rome"}, "I1", "confirmed"),
        ("old@imp.test", "Old Renamed", "enabled", {"city": "Rome"}, "I3", "confirmed"),
    ]
    assert people(app, "new.one@%") == [
        ("new.one@imp.test", "New One", "enabled", {"city": "Oslo", "n": 2}, "I1", "confirmed"),
        ("new.one@imp.test", "New One", "enabled", {"city": "Oslo", "n": 2}, "I3", "confirmed"),
    ]
    # Overwriting with an empty row replaces the name with the derived one and clears the attributes.
    app.run_import("email,name,attributes\nold@imp.test,,\n", {"mode": "subscribe", "delim": ",", "lists": [lists["I1"]], "overwrite": True})
    assert people(app, "old@%") == [
        ("old@imp.test", "Old", "enabled", {}, "I1", "unconfirmed"),
        ("old@imp.test", "Old", "enabled", {}, "I3", "confirmed"),
    ]


def test_overwrite_switches_are_independent(imp):
    app, lists = imp
    row = 'email,name,attributes\nold@imp.test,Changed,"{""b"": 2}"\n'
    base = {"mode": "subscribe", "delim": ",", "lists": [lists["I1"], lists["I2"], lists["I3"]]}

    app.run_import(row, {**base, "subscription_status": "unsubscribed", "overwrite_userinfo": True})
    assert people(app, "old@%") == [
        ("old@imp.test", "Changed", "enabled", {"b": 2}, "I1", "confirmed"),
        ("old@imp.test", "Changed", "enabled", {"b": 2}, "I2", "unsubscribed"),
        ("old@imp.test", "Changed", "enabled", {"b": 2}, "I3", "unsubscribed"),
    ]
    app.run_import('email,name,attributes\nold@imp.test,Again,"{""c"": 3}"\n', {**base, "subscription_status": "confirmed", "overwrite_subscription_status": True})
    assert people(app, "old@%") == [
        ("old@imp.test", "Changed", "enabled", {"b": 2}, "I1", "confirmed"),
        ("old@imp.test", "Changed", "enabled", {"b": 2}, "I2", "confirmed"),
        ("old@imp.test", "Changed", "enabled", {"b": 2}, "I3", "confirmed"),
    ]
    # Duplicate rows in one file: with overwrite the last one wins, without it the first one does.
    app.run_import("email,name\ndup@imp.test,First\nDUP@imp.test,Second\n", {**base, "lists": [lists["I1"]], "overwrite": True})
    app.run_import("email,name\nkeep@imp.test,First\nkeep@imp.test,Second\n", {**base, "lists": [lists["I1"]]})
    assert app.q("SELECT email, name FROM subscribers WHERE email IN ('dup@imp.test', 'keep@imp.test') ORDER BY 1") == [
        ("dup@imp.test", "Second"), ("keep@imp.test", "First")]


def test_blocklist_mode(imp):
    app, lists = imp
    app.run_import("email,name\nnew.one@imp.test,New One\n", {"mode": "subscribe", "delim": ",", "lists": [lists["I1"], lists["I2"]], "subscription_status": "confirmed"})
    final = app.run_import("email,name\nblk@imp.test,Blk\nNEW.ONE@imp.test,Other Name\n", {"mode": "blocklist", "delim": ",", "lists": [lists["I3"]], "subscription_status": "confirmed"})
    assert_import(final, {"name": "subscribers.csv", "total": 2, "imported": 2, "status": "finished"})
    assert people(app, "blk@%") + people(app, "new.one@%") == [
        ("blk@imp.test", "Blk", "blocklisted", {}, None, None),
        ("new.one@imp.test", "New One", "blocklisted", {}, "I1", "unsubscribed"),
        ("new.one@imp.test", "New One", "blocklisted", {}, "I2", "unsubscribed"),
    ]
    assert people(app, "old@%")[0][2] == "enabled"
    # Re-subscribing a blocklisted address keeps it blocklisted and unsubscribed.
    app.run_import("email,name\nblk@imp.test,Blk Back\nfresh@imp.test,Fresh\n", {"mode": "subscribe", "delim": ",", "lists": [lists["I1"]], "overwrite": True, "subscription_status": "confirmed"})
    assert people(app, "blk@%") + people(app, "fresh@%") == [
        ("blk@imp.test", "Blk Back", "blocklisted", {}, "I1", "unsubscribed"),
        ("fresh@imp.test", "Fresh", "enabled", {}, "I1", "confirmed"),
    ]


def test_delimiters_quoting_and_column_order(imp):
    app, lists = imp
    target = {"mode": "subscribe", "lists": [lists["I3"]]}
    assert_json(app.run_import("email;name\nsemi@imp.test;Semi Colon\n", {**target, "delim": ";"})["imported"], 1)
    assert_json(app.run_import("email\tname\ntab@imp.test\tTab Person\n", {**target, "delim": "\t"})["imported"], 1)
    assert_json(app.run_import('name,email\r\n"Last, First",quote@imp.test\r\n', {**target, "delim": ","})["imported"], 1)
    assert app.q("SELECT email, name FROM subscribers WHERE email IN ('semi@imp.test', 'tab@imp.test', 'quote@imp.test') ORDER BY 1") == [
        ("quote@imp.test", "Last, First"), ("semi@imp.test", "Semi Colon"), ("tab@imp.test", "Tab Person")]

    before = people(app)
    final = app.run_import("email;name\nwrong@imp.test;Wrong\nold@imp.test;Old Semi\n", {**target, "delim": ","})
    assert_import(final, {"name": "subscribers.csv", "total": 2, "imported": 0, "status": "failed"})
    assert people(app) == before


def test_headers_and_empty_files(imp):
    app, lists = imp
    target = {"mode": "subscribe", "delim": ",", "lists": [lists["I1"]]}
    assert_import(app.run_import("\ufeffemail,name\nbom@imp.test,Bom\n", target), {"name": "subscribers.csv", "total": 1, "imported": 1, "status": "finished"})
    assert app.run_import("Email , NAME\nupper@imp.test,U\n", target)["status"] == "failed"
    assert app.run_import("", target, filename="empty.csv")["status"] == "failed"
    assert_import(app.run_import("email\n", target, filename="header.csv"), {"name": "header.csv", "total": 0, "imported": 0, "status": "finished"})
    assert [r[0] for r in app.q("SELECT email FROM subscribers WHERE email LIKE '%%@imp.test' ORDER BY 1")] == ["bom@imp.test", "old@imp.test"]


def test_zip_archive(imp):
    app, lists = imp
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("inner.csv", "email,name\nzip1@imp.test,Zip One\nzip2@imp.test,Zip Two\n")
    final = app.run_import(buffer.getvalue(), {"mode": "subscribe", "delim": ",", "lists": [lists["I2"]], "subscription_status": "confirmed"},
                           filename="pack.zip", ctype="application/zip")
    assert_import(final, {"name": "pack.zip", "total": 2, "imported": 2, "status": "finished"})
    assert people(app, "zip%") == [("zip1@imp.test", "Zip One", "enabled", {}, "I2", "confirmed"), ("zip2@imp.test", "Zip Two", "enabled", {}, "I2", "confirmed")]

    before = people(app)
    status, payload = app.start_import(b"not a zip", {"mode": "subscribe", "delim": ",", "lists": [lists["I2"]]}, filename="junk.zip", ctype="application/zip")
    assert status == 500 and set(payload) == {"message"}
    assert_import(app.get("/api/import/subscribers"), {"name": "junk.zip", "total": 0, "imported": 0, "status": "failed"})
    assert people(app) == before


def test_rejected_import_requests(imp):
    app, lists = imp
    app.run_import("email\nfirst@imp.test\n", {"mode": "subscribe", "delim": ",", "lists": [lists["I1"]]}, filename="done.csv")
    before = people(app)
    good = {"mode": "subscribe", "delim": ",", "lists": [lists["I1"]]}
    for params, expected in (
        ({"mode": "subscribe", "delim": ","}, 403),
        ({**good, "mode": "frob"}, 400),
        ({**good, "delim": ",,"}, 400),
        ({**good, "delim": ""}, 400),
        ({**good, "subscription_status": "nah"}, 400),
    ):
        status, payload = app.start_import("email\nx@imp.test\n", params)
        assert status == expected, (params, status, payload)
        assert set(payload) == {"message"}
    for fields, files in (({"params": "{"}, {"file": ("a.csv", "text/csv", b"email\nx@imp.test\n")}),
                          ({"params": '{"mode": "subscribe", "delim": ",", "lists": [%d]}' % lists["I1"]}, {})):
        body, ctype = multipart(fields, files)
        status, _ = app.call("POST", "/api/import/subscribers", body, ctype=ctype)
        assert status == 400
    assert_import(app.get("/api/import/subscribers"), {"name": "done.csv", "total": 1, "imported": 1, "status": "finished"})
    assert people(app) == before


def test_upstream_fixture_import_cases(app):
    """Adapted from upstream frontend/cypress/e2e/import.cy.js: four imports of the same 100-row file."""
    data = (FIXTURES / "subs.csv").read_text()
    list_id = app.create_list("Imported")["id"]
    counts = ("SELECT s.status::text, sl.status::text, COUNT(*) FROM subscribers s JOIN subscriber_lists sl ON sl.subscriber_id = s.id "
              "WHERE sl.list_id = %s GROUP BY 1, 2 ORDER BY 1, 2")
    cases = [
        ({"mode": "subscribe", "subscription_status": "unconfirmed", "overwrite": True}, [("enabled", "unconfirmed", 100)]),
        ({"mode": "subscribe", "subscription_status": "confirmed", "overwrite": True}, [("enabled", "confirmed", 100)]),
        ({"mode": "subscribe", "subscription_status": "unconfirmed", "overwrite": False}, [("enabled", "confirmed", 100)]),
        ({"mode": "blocklist", "subscription_status": "unsubscribed", "overwrite": False}, [("blocklisted", "unsubscribed", 100)]),
    ]
    for params, expected in cases:
        final = app.run_import(data, {**params, "delim": ",", "lists": [list_id]}, filename="subs.csv")
        assert_import(final, {"name": "subs.csv", "total": 100, "imported": 100, "status": "finished"})
        assert app.q(counts, list_id) == expected, params
        assert app.q("SELECT COUNT(*) FROM subscribers") == [(102,)]
    assert app.q("SELECT name, attribs FROM subscribers WHERE email = 'user7@mail.com'") == [
        ("First7 Last7", {"age": 56, "city": "Bangalore", "clientId": "DAXX54"})]
    # The imported attributes are queryable; the selection matches the database's own evaluation.
    expression = "(subscribers.attribs->>'age')::INT > 60"
    found = sorted(item["email"] for item in app.query_subscribers(query=expression)["results"])
    assert found == sorted(r[0] for r in app.q(f"SELECT email FROM subscribers WHERE {expression}")) and found
