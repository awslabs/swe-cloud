"""Organization isolation: one application's credential against the other organization's directory."""

import threading

from support import SEED_USERS, assert_profile, assert_row, ok, refused


def test_list_scoped_to_credentials_organization(directory):
    before = directory.users_snapshot()
    for path in (
        "/api/get-users?owner=globex",
        "/api/get-users?owner=globex&p=1&pageSize=10",
        "/api/get-users",
        "/api/get-users?owner=built-in",
        "/api/get-users?owner=nowhere",
    ):
        refused(directory.get(path, "acme"))
    refused(directory.get("/api/get-users?owner=acme", "globex"))
    assert directory.users_snapshot() == before


def test_get_other_organizations_user_refused(directory):
    for path in (
        "/api/get-user?id=globex/dana",
        "/api/get-user?id=globex/gus",
        "/api/get-user?id=globex/dana&owner=acme",
        "/api/get-user?id=built-in/admin",
    ):
        resp = directory.get(path, "acme")
        refused(resp)
        assert b"globex-dana-bio" not in resp.raw and b"dana@globex.test" not in resp.raw
    refused(directory.get("/api/get-user?id=acme/dana", "globex"))
    # an identifier that names no record yields no data either way
    ok(directory.get("/api/get-user?id=globex/nobody", "acme"), None)


def test_get_selects_record_by_id_not_owner_query(directory):
    # The owner query parameter does not redirect the lookup: id decides both authorization and the record.
    user = ok(directory.get("/api/get-user?id=acme/dana&owner=globex", "acme"))
    assert_profile(user, "acme", "dana", password=True, **SEED_USERS[("acme", "dana")])
    user = ok(directory.get("/api/get-user?id=globex/dana&owner=acme", "globex"))
    assert_profile(user, "globex", "dana", **SEED_USERS[("globex", "dana")])


def test_same_name_users_are_independent_records(directory):
    ok(directory.post("/api/add-user", {"owner": "acme", "name": "alex", "displayName": "Alex A", "email": "alex@shared.test", "title": "TA"}, "acme"), "Affected")
    ok(directory.post("/api/add-user", {"owner": "globex", "name": "alex", "displayName": "Alex G", "email": "alex@shared.test", "bio": "BG"}, "globex"), "Affected")
    rows = directory.sql('SELECT owner, id FROM "user" WHERE name = %s ORDER BY owner', "alex")
    assert [r[0] for r in rows] == ["acme", "globex"] and rows[0][1] != rows[1][1]
    assert_row(directory.row("acme", "alex"), "acme", "alex", displayName="Alex A", email="alex@shared.test", title="TA")
    assert_row(directory.row("globex", "alex"), "globex", "alex", displayName="Alex G", email="alex@shared.test", bio="BG")

    globex_before = directory.users_snapshot(owner="globex")
    a = directory.user("acme", "alex")
    ok(directory.post("/api/update-user?id=acme/alex", {**a, "title": "Changed", "bio": "acme-only"}, "acme"), "Affected")
    assert_row(directory.row("acme", "alex"), "acme", "alex", displayName="Alex A", email="alex@shared.test", title="Changed", bio="acme-only")
    assert directory.users_snapshot(owner="globex") == globex_before
    assert_profile(directory.user("globex", "alex"), "globex", "alex", displayName="Alex G", email="alex@shared.test", bio="BG")

    acme_before = directory.users_snapshot(owner="acme")
    ok(directory.post("/api/delete-user", {"owner": "globex", "name": "alex"}, "globex"), "Affected")
    assert directory.row("globex", "alex") is None
    assert directory.users_snapshot(owner="acme") == acme_before
    ok(directory.get("/api/get-user?id=globex/alex", "globex"), None)
    assert_profile(directory.user("acme", "alex"), "acme", "alex", displayName="Alex A", email="alex@shared.test", title="Changed", bio="acme-only")


def test_add_into_other_organization_refused(directory):
    before = directory.users_snapshot()
    for body in (
        {"owner": "globex", "name": "mallory"},
        {"owner": "globex", "name": "dana", "displayName": "overwrite?"},
        {"name": "mallory"},
        {"owner": "nowhere", "name": "mallory"},
        {"owner": "built-in", "name": "mallory"},
    ):
        refused(directory.post("/api/add-user", body, "acme"))
        assert directory.users_snapshot() == before, body
    refused(directory.post("/api/add-user?owner=acme", {"owner": "globex", "name": "mallory"}, "acme"))
    refused(directory.post("/api/add-user", b"not json", "acme"))
    assert directory.users_snapshot() == before


def test_update_other_organizations_user_refused(directory):
    target = directory.user("globex", "dana")
    mine = directory.user("acme", "dana")
    before = directory.users_snapshot()
    attempts = [
        ("/api/update-user?id=globex/dana", {**target, "displayName": "HACKED"}),
        ("/api/update-user?id=globex/dana", {**target, "owner": "acme", "displayName": "HACKED"}),
        ("/api/update-user?id=globex/dana&owner=acme", {**target, "owner": "acme", "displayName": "HACKED"}),
        ("/api/update-user?id=globex/dana&columns=displayName", {"displayName": "HACKED"}),
        ("/api/update-user?id=acme/dana", {**target, "displayName": "HACKED"}),
        ("/api/update-user?id=acme/dana", {**mine, "owner": "globex", "displayName": "HACKED"}),
        ("/api/update-user?id=acme/dana&columns=title", {"owner": "globex", "title": "HACKED"}),
        ("/api/update-user?id=acme/dana&owner=globex", {**mine, "displayName": "HACKED"}),
    ]
    for path, body in attempts:
        refused(directory.post(path, body, "acme"))
        assert directory.users_snapshot() == before, path
    # the legitimate owner may still change its own record
    ok(directory.post("/api/update-user?id=globex/dana&owner=globex", {**target, "title": "VP"}, "globex"), "Affected")
    assert_row(directory.row("globex", "dana"), "globex", "dana", **{**SEED_USERS[("globex", "dana")], "title": "VP"})


def test_moving_a_user_between_organizations_refused(directory):
    target = directory.user("globex", "gus")
    before = directory.users_snapshot()
    refused(directory.post("/api/update-user?id=globex/gus", {**target, "owner": "acme"}, "globex"))
    assert directory.users_snapshot() == before


def test_delete_other_organizations_user_refused(directory):
    before = directory.users_snapshot()
    for path, body in (
        ("/api/delete-user", {"owner": "globex", "name": "dana"}),
        ("/api/delete-user?id=acme/dana", {"owner": "globex", "name": "dana"}),
        ("/api/delete-user?id=globex/dana", {"owner": "acme", "name": "dana"}),
        ("/api/delete-user", {"name": "dana"}),
    ):
        refused(directory.post(path, body, "acme"))
        assert directory.users_snapshot() == before, (path, body)
    refused(directory.post("/api/delete-user", {"owner": "acme", "name": "erin"}, "globex"))
    assert directory.users_snapshot() == before


def test_concurrent_updates_stay_in_their_rows(directory):
    ok(directory.post("/api/add-user", {"owner": "acme", "name": "alex", "displayName": "Alex A"}, "acme"), "Affected")
    ok(directory.post("/api/add-user", {"owner": "globex", "name": "alex", "displayName": "Alex G"}, "globex"), "Affected")
    bodies = {org: directory.user(org, "alex") for org in ("acme", "globex")}
    failures = []

    def worker(org, i):
        try:
            body = {**bodies[org], "bio": f"{org}-sentinel", "title": f"{org}-{i % 3}", "location": org.upper()}
            ok(directory.post(f"/api/update-user?id={org}/alex", body, org), "Affected")
        except BaseException as exc:  # reported below
            failures.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(org, i)) for i in range(8) for org in ("acme", "globex")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(40)
    assert not failures, failures
    for org, display in (("acme", "Alex A"), ("globex", "Alex G")):
        row = directory.row(org, "alex")
        assert row["bio"] == f"{org}-sentinel" and row["location"] == org.upper(), row
        assert row["title"] in {f"{org}-{k}" for k in range(3)}, row
        assert row["display_name"] == display and row["id"] == bodies[org]["id"]
