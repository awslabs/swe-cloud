"""The credential's own organization: its settings, user count and sorted users."""

import pytest

from support import is_count, ok, refused

ORG_FIELDS = ("owner", "name", "displayName", "websiteUrl", "passwordType", "countryCodes", "languages", "passwordOptions")


def organization(d, org="acme"):
    return ok(d.get(f"/api/get-organization?id=admin/{org}", org))


def stored(d, org="acme"):
    return d.sql("SELECT owner, name, display_name, website_url, password_type, password_options FROM organization WHERE name = %s", org)


def test_get_own_organization(directory):
    got = organization(directory)
    assert {k: got[k] for k in ORG_FIELDS} == {"owner": "admin", "name": "acme", "displayName": "Acme", "websiteUrl": "",
                                                "passwordType": "bcrypt", "countryCodes": ["US"], "languages": ["en"], "passwordOptions": None}


def test_update_own_organization(directory):
    body = {**organization(directory), "displayName": "Acme Corp", "websiteUrl": "https://acme.test"}
    ok(directory.post("/api/update-organization?id=admin/acme", body), "Affected")
    assert stored(directory) == [("admin", "acme", "Acme Corp", "https://acme.test", "bcrypt", "null")]
    assert organization(directory)["displayName"] == "Acme Corp"


def test_organization_cannot_be_renamed(directory):
    before = stored(directory)
    refused(directory.post("/api/update-organization?id=admin/acme", {**organization(directory), "name": "acme2"}))
    assert stored(directory) == before and stored(directory, "acme2") == []


def test_other_organization_is_refused(directory):
    refused(directory.get("/api/get-organization?id=admin/globex"))
    before = stored(directory, "globex")
    refused(directory.post("/api/update-organization?id=admin/globex", {**organization(directory, "globex"), "displayName": "Hijack"}))
    assert stored(directory, "globex") == before


def test_user_count(directory):
    assert is_count(ok(directory.get("/api/get-user-count?owner=acme&isOnline=")), 2)
    ok(directory.post("/api/add-user", {"owner": "acme", "name": "new1"}), "Affected")
    assert is_count(ok(directory.get("/api/get-user-count?owner=acme&isOnline=")), 3)
    assert is_count(ok(directory.get("/api/get-user-count?owner=acme&isOnline=1")), 0)
    assert is_count(ok(directory.get("/api/get-user-count?owner=globex&isOnline=", "globex")), 2)


def test_sorted_users_descend_and_limit(directory):
    for name in ("amy", "zed", "max"):
        ok(directory.post("/api/add-user", {"owner": "acme", "name": name}), "Affected")
    got = ok(directory.get("/api/get-sorted-users?owner=acme&sorter=name&limit=3"))
    assert [u["name"] for u in got] == ["zed", "max", "erin"]


def test_sorted_users_rejects_unsafe_sorter(directory):
    reply = directory.get("/api/get-sorted-users?owner=acme&sorter=name%20desc&limit=3")
    assert reply.code == 200 and reply.json["status"] == "error" and reply.json.get("data") is None, reply
    assert "invalid sorter field: name desc" in reply.json["msg"], reply


@pytest.mark.timeout(150)
def test_organization_edits_survive_restart(directory):
    body = {**organization(directory), "displayName": "Acme Corp", "passwordOptions": ["AtLeast8"]}
    ok(directory.post("/api/update-organization?id=admin/acme", body), "Affected")
    directory.stop()
    directory.start()
    assert stored(directory) == [("admin", "acme", "Acme Corp", "", "bcrypt", '["AtLeast8"]')]
