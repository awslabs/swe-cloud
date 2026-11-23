"""Roles of an organization: add, read, list, update, rename and delete."""

from support import envelope, ok, refused


def role(name, **fields):
    return {"owner": "acme", "name": name, "displayName": name.title(), "description": "", "users": [], "groups": [], "roles": [],
            "domains": [], "isEnabled": True, **fields}


ROLE_FIELDS = ("owner", "name", "displayName", "description", "users", "groups", "roles", "domains", "isEnabled")


def view(r):
    return {k: r[k] for k in ROLE_FIELDS}


def stored(d):
    return d.sql("SELECT owner, name, display_name, description, users, groups, roles, is_enabled FROM role ORDER BY owner, name")


def test_add_and_get_role(directory):
    ok(directory.post("/api/add-role", role("viewer", description="read only", users=["acme/dana"])), "Affected")
    assert view(ok(directory.get("/api/get-role?id=acme/viewer"))) == view(role("viewer", description="read only", users=["acme/dana"]))


def test_role_is_stored_with_member_lists(directory):
    ok(directory.post("/api/add-role", role("viewer", users=["acme/dana", "acme/erin"], groups=["acme/eng"])), "Affected")
    assert stored(directory) == [("acme", "viewer", "Viewer", "", '["acme/dana","acme/erin"]', '["acme/eng"]', "[]", True)]


def test_get_missing_role_is_ok_null(directory):
    ok(directory.get("/api/get-role?id=acme/none"), None)


def test_list_roles(directory):
    for name in ("viewer", "editor"):
        ok(directory.post("/api/add-role", role(name)), "Affected")
    assert sorted(r["name"] for r in ok(directory.get("/api/get-roles?owner=acme"))) == ["editor", "viewer"]
    assert ok(directory.get("/api/get-roles?owner=globex", "globex")) == []


def test_update_role_members(directory):
    ok(directory.post("/api/add-role", role("viewer", users=["acme/dana"])), "Affected")
    body = {**ok(directory.get("/api/get-role?id=acme/viewer")), "users": ["acme/erin"], "displayName": "Readers", "isEnabled": False}
    ok(directory.post("/api/update-role?id=acme/viewer", body), "Affected")
    assert stored(directory) == [("acme", "viewer", "Readers", "", '["acme/erin"]', "[]", "[]", False)]


def test_rename_role_updates_roles_that_include_it(directory):
    ok(directory.post("/api/add-role", role("viewer")), "Affected")
    ok(directory.post("/api/add-role", role("editor", roles=["acme/viewer"])), "Affected")
    ok(directory.post("/api/update-role?id=acme/viewer", {**ok(directory.get("/api/get-role?id=acme/viewer")), "name": "reader"}), "Affected")
    ok(directory.get("/api/get-role?id=acme/viewer"), None)
    assert ok(directory.get("/api/get-role?id=acme/editor"))["roles"] == ["acme/reader"]


def test_delete_role(directory):
    ok(directory.post("/api/add-role", role("viewer")), "Affected")
    ok(directory.post("/api/add-role", role("editor")), "Affected")
    ok(directory.post("/api/delete-role", {"owner": "acme", "name": "viewer"}), "Affected")
    assert [r[1] for r in stored(directory)] == ["editor"]
    ok(directory.post("/api/delete-role", {"owner": "acme", "name": "viewer"}), "Unaffected")


def test_duplicate_role_is_rejected(directory):
    ok(directory.post("/api/add-role", role("viewer", displayName="First")), "Affected")
    reply = envelope(directory.post("/api/add-role", role("viewer", displayName="Second")))
    assert reply["status"] == "error" and reply.get("data") is None, reply
    assert [r[2] for r in stored(directory)] == ["First"]


def test_role_for_other_organization_is_refused(directory):
    refused(directory.post("/api/add-role", {**role("ops"), "owner": "globex"}))
    assert stored(directory) == []
