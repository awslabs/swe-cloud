"""Groups of an organization: add, read, list, update, delete, and user membership."""

from support import envelope, error, is_count, ok, refused


def group(name, parent=None, **fields):
    return {"owner": "acme", "name": name, "displayName": name.title(), "type": "Virtual", "parentId": parent or "acme",
            "isTopGroup": parent is None, "isEnabled": True, **fields}


GROUP_FIELDS = ("owner", "name", "displayName", "type", "parentId", "isTopGroup", "isEnabled", "manager", "contactEmail")


def view(g):
    return {k: g[k] for k in GROUP_FIELDS}


def stored(d):
    return d.sql('SELECT owner, name, display_name, type, parent_id, is_top_group, is_enabled, manager, contact_email FROM "group" ORDER BY owner, name')


def join(d, name, *groups):
    user = d.user("acme", name)
    ok(d.post(f"/api/update-user?id=acme/{name}", {**user, "groups": list(groups)}), "Affected")


def test_add_and_get_group(directory):
    ok(directory.post("/api/add-group", group("eng", manager="dana", contactEmail="eng@acme.test")), "Affected")
    got = ok(directory.get("/api/get-group?id=acme/eng"))
    assert view(got) == {"owner": "acme", "name": "eng", "displayName": "Eng", "type": "Virtual", "parentId": "acme", "isTopGroup": True,
                         "isEnabled": True, "manager": "dana", "contactEmail": "eng@acme.test"}


def test_group_is_stored(directory):
    ok(directory.post("/api/add-group", group("eng")), "Affected")
    ok(directory.post("/api/add-group", group("web", parent="eng", type="Physical")), "Affected")
    assert stored(directory) == [("acme", "eng", "Eng", "Virtual", "acme", True, True, "", ""),
                                 ("acme", "web", "Web", "Physical", "eng", False, True, "", "")]


def test_get_missing_group_is_ok_null(directory):
    ok(directory.get("/api/get-group?id=acme/nothing"), None)


def test_list_groups_whole_and_paginated(directory):
    for name in ("g1", "g2", "g3"):
        ok(directory.post("/api/add-group", group(name)), "Affected")
    listing = ok(directory.get("/api/get-groups?owner=acme"))
    assert sorted(g["name"] for g in listing) == ["g1", "g2", "g3"]
    doc = envelope(directory.get("/api/get-groups?owner=acme&p=2&pageSize=2"))
    assert doc["status"] == "ok" and is_count(doc["data2"], 3) and len(doc["data"]) == 1, doc
    assert ok(directory.get("/api/get-groups?owner=globex", "globex")) == []


def test_update_group_fields(directory):
    ok(directory.post("/api/add-group", group("eng")), "Affected")
    body = {**ok(directory.get("/api/get-group?id=acme/eng")), "displayName": "Engineering", "contactEmail": "eng@acme.test", "isEnabled": False}
    ok(directory.post("/api/update-group?id=acme/eng", body), "Affected")
    assert stored(directory) == [("acme", "eng", "Engineering", "Virtual", "acme", True, False, "", "eng@acme.test")]


def test_rename_group(directory):
    ok(directory.post("/api/add-group", group("eng")), "Affected")
    ok(directory.post("/api/update-group?id=acme/eng", {**ok(directory.get("/api/get-group?id=acme/eng")), "name": "rnd"}), "Affected")
    ok(directory.get("/api/get-group?id=acme/eng"), None)
    assert ok(directory.get("/api/get-group?id=acme/rnd"))["displayName"] == "Eng"


def test_group_name_is_required(directory):
    error(directory.post("/api/add-group", group("")), "Field 'name' cannot be empty")
    assert stored(directory) == []


def test_duplicate_group_is_rejected(directory):
    ok(directory.post("/api/add-group", group("eng", displayName="First")), "Affected")
    reply = envelope(directory.post("/api/add-group", group("eng", displayName="Second")))
    assert reply["status"] == "error" and reply.get("data") is None, reply
    assert stored(directory) == [("acme", "eng", "First", "Virtual", "acme", True, True, "", "")]


def test_users_of_a_group(directory):
    ok(directory.post("/api/add-group", group("eng")), "Affected")
    join(directory, "erin", "acme/eng")
    assert directory.user("acme", "erin")["groups"] == ["acme/eng"]
    assert [u["name"] for u in ok(directory.get("/api/get-users?owner=acme&groupName=eng"))] == ["erin"]
    doc = envelope(directory.get("/api/get-users?owner=acme&groupName=eng&p=1&pageSize=5"))
    assert [u["name"] for u in doc["data"]] == ["erin"] and is_count(doc["data2"], 1), doc


def test_group_with_subgroups_cannot_be_deleted(directory):
    ok(directory.post("/api/add-group", group("eng")), "Affected")
    ok(directory.post("/api/add-group", group("web", parent="eng")), "Affected")
    before = stored(directory)
    error(directory.post("/api/delete-group", {"owner": "acme", "name": "eng"}), "group has children group")
    assert stored(directory) == before


def test_group_with_users_cannot_be_deleted(directory):
    ok(directory.post("/api/add-group", group("eng")), "Affected")
    join(directory, "erin", "acme/eng")
    error(directory.post("/api/delete-group", {"owner": "acme", "name": "eng"}), "group has users")
    join(directory, "erin")
    ok(directory.post("/api/delete-group", {"owner": "acme", "name": "eng"}), "Affected")
    assert stored(directory) == []


def test_delete_leaf_group(directory):
    ok(directory.post("/api/add-group", group("eng")), "Affected")
    ok(directory.post("/api/add-group", group("web", parent="eng")), "Affected")
    ok(directory.post("/api/delete-group", {"owner": "acme", "name": "web"}), "Affected")
    assert [r[1] for r in stored(directory)] == ["eng"]
    ok(directory.post("/api/delete-group", {"owner": "acme", "name": "web"}), "Unaffected")


def test_group_for_other_organization_is_refused(directory):
    refused(directory.post("/api/add-group", {**group("ops"), "owner": "globex", "parentId": "globex"}))
    assert stored(directory) == []
