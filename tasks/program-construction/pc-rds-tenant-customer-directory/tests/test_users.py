"""Single directory operations within one tenant: add, get, update, delete, with stored rows inspected."""

from datetime import datetime, timezone

from support import (
    SEED_USERS,
    assert_profile,
    assert_recent,
    assert_row,
    error,
    is_uuid,
    ok,
    parse_time,
)

FULL = {
    "displayName": "Zoë 東京 \"Q\"",
    "firstName": "Zoe",
    "lastName": "Tanaka",
    "email": "zoe@acme.test",
    "title": "Engineer",
    "bio": "line one\nline two",
    "affiliation": "Acme R&D",
    "tag": "staff",
    "homepage": "https://zoe.example.test/",
    "location": "Kyoto",
    "region": "JP",
    "language": "ja",
    "gender": "female",
    "birthday": "1990-04-05",
    "education": "MSc",
}


def test_get_seeded_users_masks_password(directory):
    for (owner, name), fields in SEED_USERS.items():
        user = directory.user(owner, name)
        assert_profile(user, owner, name, password=(owner, name) == ("acme", "dana"), **fields)
        row = directory.row(owner, name)
        assert user["id"] == row["id"] and is_uuid(user["id"])
        assert user["createdTime"] == row["created_time"] and user["updatedTime"] == row["updated_time"]
    # the stored secret is never the masked placeholder handed out
    stored = directory.sql('SELECT password FROM "user" WHERE owner = %s AND name = %s', "acme", "dana")[0][0]
    assert stored not in ("", "***")


def test_get_missing_user_is_ok_null(directory):
    before = directory.users_snapshot()
    ok(directory.get("/api/get-user?id=acme/nobody"), None)
    assert directory.users_snapshot() == before


def test_add_minimal_user(directory):
    since = datetime.now(timezone.utc)
    ok(directory.post("/api/add-user", {"owner": "acme", "name": "alex"}), "Affected")
    row = directory.row("acme", "alex")
    assert_row(row, "acme", "alex")
    assert is_uuid(row["id"])
    assert_recent(row["created_time"], since)
    assert row["updated_time"] == row["created_time"]
    user = directory.user("acme", "alex")
    assert_profile(user, "acme", "alex")
    assert (user["id"], user["createdTime"], user["updatedTime"]) == (row["id"], row["created_time"], row["updated_time"])


def test_add_user_with_all_profile_fields(directory):
    ok(directory.post("/api/add-user", {"owner": "acme", "name": "zoe", "password": "Zoe-pass-77", **FULL}), "Affected")
    assert_row(directory.row("acme", "zoe"), "acme", "zoe", **FULL)
    assert_profile(directory.user("acme", "zoe"), "acme", "zoe", password=True, **FULL)
    stored = directory.sql('SELECT password FROM "user" WHERE owner = %s AND name = %s', "acme", "zoe")[0][0]
    assert stored not in ("", "***")


def test_add_keeps_supplied_created_time_and_update_refreshes_updated_time(directory):
    supplied = parse_time("2020-05-06T07:08:09Z")
    ok(directory.post("/api/add-user", {"owner": "acme", "name": "old", "createdTime": "2020-05-06T07:08:09Z"}), "Affected")
    row = directory.row("acme", "old")
    assert parse_time(row["created_time"]) == supplied and parse_time(row["updated_time"]) == supplied, row
    user = directory.user("acme", "old")
    assert (user["createdTime"], user["updatedTime"]) == (row["created_time"], row["updated_time"])
    since = datetime.now(timezone.utc)
    ok(directory.post("/api/update-user?id=acme/old", {**user, "title": "Touched"}), "Affected")
    after = directory.row("acme", "old")
    assert after["title"] == "Touched"
    assert after["created_time"] == row["created_time"]
    assert_recent(after["updated_time"], since)
    user = directory.user("acme", "old")
    assert (user["createdTime"], user["updatedTime"]) == (after["created_time"], after["updated_time"])


def test_add_validation_failures_store_nothing(directory):
    invalid_name = (
        "The username may only contain alphanumeric characters, underlines or hyphens, cannot have consecutive hyphens "
        "or underlines, and cannot begin or end with a hyphen or underline."
    )
    cases = [
        ({"owner": "acme", "name": "dana", "displayName": "again"}, "Username already exists"),
        ({"owner": "acme", "name": ""}, "Field 'name' cannot be empty"),
        ({"owner": "acme", "displayName": "nameless"}, "Field 'name' cannot be empty"),
        ({"owner": "acme", "name": "dup-mail", "email": "erin@acme.test"}, "Email already exists"),
        ({"owner": "acme", "name": "ph", "phone": "abc"}, "Phone number is invalid"),
    ]
    for name in ("bad name", "-lead", "trail-", "a__b", "a..b", "x@y.test", "名字"):
        cases.append(({"owner": "acme", "name": name}, invalid_name))
    before = directory.users_snapshot()
    for body, msg in cases:
        error(directory.post("/api/add-user", body), msg)
        assert directory.users_snapshot() == before, body


def test_add_accepts_username_forms(directory):
    names = ["a", "A1", "Mixed.Case_1", "a-b_c.d", "1abc"]
    for name in names:
        ok(directory.post("/api/add-user", {"owner": "acme", "name": name}), "Affected")
    assert directory.names("acme") == sorted(["dana", "erin", *names])


def test_full_update_replaces_profile(directory):
    original = directory.user("acme", "dana")
    row_before = directory.row("acme", "dana")
    body = dict(original)
    body.update({"displayName": "Dana A.", "title": "Lead", "location": "Oslo"})
    body.pop("email")
    body.pop("bio")
    body["createdTime"] = "1999-01-01T00:00:00Z"
    ok(directory.post("/api/update-user?id=acme/dana", body), "Affected")
    expected = {"displayName": "Dana A.", "title": "Lead", "location": "Oslo", "affiliation": "Acme Labs"}
    row = directory.row("acme", "dana")
    assert_row(row, "acme", "dana", **expected)  # omitted email and bio are cleared
    assert row["id"] == row_before["id"]
    assert row["created_time"] == row_before["created_time"]
    assert parse_time(row["updated_time"]) >= parse_time(row_before["updated_time"])
    user = directory.user("acme", "dana")
    assert_profile(user, "acme", "dana", password=True, **expected)
    assert user["updatedTime"] == row["updated_time"]


def test_masked_password_round_trip_keeps_secret(directory):
    stored = directory.sql('SELECT password FROM "user" WHERE owner = %s AND name = %s', "acme", "dana")
    body = directory.user("acme", "dana")
    assert body["password"] == "***"
    body["bio"] = "rewritten"
    ok(directory.post("/api/update-user?id=acme/dana", body), "Affected")
    assert directory.sql('SELECT password FROM "user" WHERE owner = %s AND name = %s', "acme", "dana") == stored
    user = directory.user("acme", "dana")
    assert_profile(user, "acme", "dana", password=True, **{**SEED_USERS[("acme", "dana")], "bio": "rewritten"})


def test_update_with_columns_changes_only_listed_fields(directory):
    ok(directory.post("/api/update-user?id=acme/dana&columns=title", {"title": "Chief", "displayName": "ignored", "email": "x@acme.test"}), "Affected")
    expected = {**SEED_USERS[("acme", "dana")], "title": "Chief"}
    assert_row(directory.row("acme", "dana"), "acme", "dana", **expected)
    ok(directory.post("/api/update-user?id=acme/dana&columns=displayName,bio", {"displayName": "D", "bio": "B", "title": "ignored"}), "Affected")
    expected.update(displayName="D", bio="B")
    assert_row(directory.row("acme", "dana"), "acme", "dana", **expected)
    # a name in the body is not a listed column: no rename happens
    ok(directory.post("/api/update-user?id=acme/dana&columns=email", {"email": "new@acme.test", "name": "renamed"}), "Affected")
    expected.update(email="new@acme.test")
    assert_row(directory.row("acme", "dana"), "acme", "dana", **expected)
    assert directory.row("acme", "renamed") is None
    assert_profile(directory.user("acme", "dana"), "acme", "dana", password=True, **expected)


def test_update_requires_the_records_id(directory):
    seed = directory.user("acme", "dana")
    other = directory.user("acme", "erin")
    before = directory.users_snapshot()
    error(directory.post("/api/update-user?id=acme/dana", {"owner": "acme", "name": "dana", "displayName": "no id"}), "The ID is immutable.")
    assert directory.users_snapshot() == before
    error(directory.post("/api/update-user?id=acme/dana", {**seed, "id": other["id"], "displayName": "wrong id"}), "The ID is immutable.")
    assert directory.users_snapshot() == before
    ok(directory.post("/api/update-user?id=acme/dana", {"owner": "acme", "name": "dana", "id": seed["id"], "displayName": "Only"}), "Affected")
    assert_row(directory.row("acme", "dana"), "acme", "dana", displayName="Only")


def test_update_conflicts_and_missing_targets(directory):
    erin = directory.user("acme", "erin")
    before = directory.users_snapshot()
    error(directory.post("/api/update-user?id=acme/erin", {**erin, "email": "dana@acme.test"}), "Email already exists")
    error(directory.post("/api/update-user?id=acme/erin", {**erin, "name": "dana"}), "Username already exists")
    error(
        directory.post("/api/update-user?id=acme/erin", {**erin, "name": "bad name"}),
        "The username may only contain alphanumeric characters, underlines or hyphens, cannot have consecutive hyphens "
        "or underlines, and cannot begin or end with a hyphen or underline.",
    )
    error(directory.post("/api/update-user?id=acme/ghost", {"owner": "acme", "name": "ghost"}), "The user: acme/ghost doesn't exist")
    error(directory.post("/api/update-user", {**erin, "displayName": "no id param"}), "The user: app/app-acme doesn't exist")
    bad = directory.post("/api/update-user?id=acme/erin", b"not json")
    assert bad.code == 200 and bad.json["status"] == "error" and bad.json.get("data") is None, bad
    assert directory.users_snapshot() == before
    # an e-mail used only in the other organization is free here
    ok(directory.post("/api/update-user?id=acme/erin", {**erin, "email": "dana@globex.test"}), "Affected")
    assert directory.row("acme", "erin")["email"] == "dana@globex.test"
    assert directory.row("globex", "dana")["email"] == "dana@globex.test"


def test_rename_moves_record(directory):
    erin = directory.user("acme", "erin")
    ok(directory.post("/api/update-user?id=acme/erin", {**erin, "name": "erin2", "title": "Renamed"}), "Affected")
    assert directory.row("acme", "erin") is None
    row = directory.row("acme", "erin2")
    assert_row(row, "acme", "erin2", **{**SEED_USERS[("acme", "erin")], "title": "Renamed"})
    assert row["id"] == erin["id"]
    ok(directory.get("/api/get-user?id=acme/erin"), None)
    assert_profile(directory.user("acme", "erin2"), "acme", "erin2", **{**SEED_USERS[("acme", "erin")], "title": "Renamed"})
    assert directory.names("acme") == ["dana", "erin2"]


def test_delete_user(directory):
    ok(directory.post("/api/add-user", {"owner": "acme", "name": "temp", "displayName": "Temp"}), "Affected")
    first_id = directory.row("acme", "temp")["id"]
    others = directory.users_snapshot(without=("acme", "temp"))
    # body fields other than owner and name do not matter
    ok(directory.post("/api/delete-user", {"owner": "acme", "name": "temp", "id": "x", "displayName": "y"}), "Affected")
    assert directory.row("acme", "temp") is None
    assert directory.users_snapshot() == others
    assert directory.users_snapshot(without=("acme", "temp")) == others
    ok(directory.get("/api/get-user?id=acme/temp"), None)
    ok(directory.post("/api/delete-user", {"owner": "acme", "name": "temp"}), "Unaffected")
    ok(directory.post("/api/delete-user", {"owner": "acme"}), "Unaffected")
    assert directory.users_snapshot() == others
    ok(directory.post("/api/add-user", {"owner": "acme", "name": "temp"}), "Affected")
    again = directory.row("acme", "temp")
    assert_row(again, "acme", "temp")
    assert again["id"] != first_id
