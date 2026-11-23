"""Listing an organization's directory, whole and paginated."""

from support import SEED_USERS, assert_profile, envelope, is_count, ok


def members(listing):
    assert isinstance(listing, list), listing
    return sorted((u["owner"], u["name"]) for u in listing)


def test_list_returns_own_users_with_profiles(directory):
    for org in ("acme", "globex"):
        listing = ok(directory.get(f"/api/get-users?owner={org}", org))
        expected = sorted(key for key in SEED_USERS if key[0] == org)
        assert members(listing) == expected
        for user in listing:
            key = (user["owner"], user["name"])
            assert_profile(user, *key, password=key == ("acme", "dana"), **SEED_USERS[key])
            assert user["id"] == directory.row(*key)["id"]


def test_list_tracks_changes(directory):
    ok(directory.post("/api/add-user", {"owner": "acme", "name": "new1", "displayName": "N1"}), "Affected")
    ok(directory.post("/api/delete-user", {"owner": "acme", "name": "erin"}), "Affected")
    dana = directory.user("acme", "dana")
    ok(directory.post("/api/update-user?id=acme/dana", {**dana, "name": "dana2"}), "Affected")
    listing = ok(directory.get("/api/get-users?owner=acme"))
    assert members(listing) == [("acme", "dana2"), ("acme", "new1")]
    by_name = {u["name"]: u for u in listing}
    assert_profile(by_name["new1"], "acme", "new1", displayName="N1")
    assert_profile(by_name["dana2"], "acme", "dana2", password=True, **SEED_USERS[("acme", "dana")])
    assert members(ok(directory.get("/api/get-users?owner=globex", "globex"))) == [("globex", "dana"), ("globex", "gus")]


def add_dated(directory, org, count):
    for i in range(1, count + 1):
        ok(directory.post("/api/add-user", {"owner": org, "name": f"p{i}", "createdTime": f"2021-01-0{i}T00:00:00Z"}, org), "Affected")


def page(directory, query, org="globex"):
    doc = envelope(directory.get(f"/api/get-users?owner={org}&{query}", org))
    assert doc["status"] == "ok" and doc["msg"] == "", doc
    return members(doc["data"]), doc.get("data2")


def test_pagination_pages_newest_first(directory):
    add_dated(directory, "globex", 5)  # seeded dana and gus are newer than every p*
    seeds = [("globex", "dana"), ("globex", "gus")]
    p = lambda *n: sorted(("globex", f"p{i}") for i in n)
    expectations = {
        "p=1&pageSize=2": seeds,
        "p=2&pageSize=2": p(5, 4),
        "p=3&pageSize=2": p(3, 2),
        "p=4&pageSize=2": p(1),
        "p=2&pageSize=3": p(4, 3, 2),
        "p=1&pageSize=10": sorted(seeds + p(1, 2, 3, 4, 5)),
    }
    for query, expected in expectations.items():
        got, total = page(directory, query)
        assert got == expected, (query, got)
        assert is_count(total, 7), (query, total)


def test_pagination_bounds(directory):
    add_dated(directory, "globex", 5)
    got, total = page(directory, "p=9&pageSize=2")
    assert got == [("globex", "p1")] and is_count(total, 7)
    got, total = page(directory, "p=0&pageSize=3")
    assert got == [("globex", "dana"), ("globex", "gus"), ("globex", "p5")] and is_count(total, 7)
    # without both parameters the whole directory is returned
    for query in ("pageSize=2", "p=2"):
        got, _ = page(directory, query)
        assert len(got) == 7, (query, got)
    got, total = page(directory, "p=1&pageSize=2", org="acme")
    assert got == [("acme", "dana"), ("acme", "erin")] and is_count(total, 2)
