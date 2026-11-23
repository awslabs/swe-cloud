"""Two tenants served at once: scoped listings, ownership guards, capabilities and token management."""

from plikenv import boolean, check_file, counts, empty, error, number, ok, page

ADD_DENIED = "you are not allowed to add file to this upload"
REMOVE_DENIED = "you are not allowed to remove this upload"
REMOVE_FILE_DENIED = "you are not allowed to remove files from this upload"


def listing_ids(client, query=""):
    body = page(client.get("/me/uploads" + query))
    ids = [x["id"] for x in body["results"]]
    number(body["total"], len(ids))
    return sorted(ids), body


def test_same_filename_from_two_tenants_stays_separate(open_pair, plik):
    alice, bobby = open_pair
    payload = {alice.login: b"alpha tenant bytes\n", bobby.login: b"bravo tenant different bytes\n"}
    made = {}
    for tenant in (alice, bobby):
        up, f = tenant.upload("same-name.txt", payload[tenant.login], removable=False)
        check_file(f, "same-name.txt", payload[tenant.login])
        made[tenant.login] = (up, f)
    (ua, fa), (ub, fb) = made["alice"], made["bobby"]
    assert ua["id"] != ub["id"] and fa["id"] != fb["id"]

    assert plik.objects() == {plik.key(ua["id"], fa["id"]): payload["alice"], plik.key(ub["id"], fb["id"]): payload["bobby"]}
    assert sorted(plik.sql('SELECT id, "user", token, removable FROM uploads')) == sorted(
        [(ua["id"], "local:alice", alice.token, False), (ub["id"], "local:bobby", bobby.token, False)]
    )
    assert sorted(plik.sql("SELECT id, upload_id, name, size, status FROM files")) == sorted(
        [(fa["id"], ua["id"], "same-name.txt", 19, "uploaded"), (fb["id"], ub["id"], "same-name.txt", 29, "uploaded")]
    )
    assert sorted(plik.sql("SELECT token, user_id FROM tokens")) == sorted([(alice.token, "local:alice"), (bobby.token, "local:bobby")])

    for tenant, (up, f) in ((alice, made["alice"]), (bobby, made["bobby"])):
        resp = plik.client().get(f"/file/{up['id']}/{f['id']}/same-name.txt")
        assert resp.status == 200 and resp.body == payload[tenant.login], resp
        ids, body = listing_ids(tenant.session)
        assert ids == [up["id"]]
        entry = body["results"][0]
        assert entry["user"] == tenant.id and entry["token"] == tenant.token, entry
        assert entry["uploadToken"] == up["uploadToken"]
        assert [x["id"] for x in entry["files"]] == [f["id"]]
        check_file(entry["files"][0], "same-name.txt", payload[tenant.login])
        boolean(entry["removable"], False)


def test_foreign_token_and_session_cannot_mutate_non_removable_upload(open_pair, plik):
    alice, bobby = open_pair
    ub, fb = bobby.upload("bravo.txt", b"bravo bytes\n", removable=False)
    before = plik.owner_state(bobby.id)
    attempts = [
        (lambda: alice.api.add_file(ub["id"], "intruder.txt", b"intrusion"), 403, ADD_DENIED),
        (lambda: alice.session.add_file(ub["id"], "intruder.txt", b"intrusion"), 403, ADD_DENIED),
        (lambda: plik.client().add_file(ub["id"], "intruder.txt", b"intrusion"), 403, ADD_DENIED),
        (lambda: alice.api.add_file(ub["id"], "bravo.txt", b"swap", suffix=f"/{fb['id']}/bravo.txt"), 403, ADD_DENIED),
        (lambda: alice.api.delete(f"/file/{ub['id']}/{fb['id']}/bravo.txt"), 403, REMOVE_FILE_DENIED),
        (lambda: alice.session.delete(f"/file/{ub['id']}/{fb['id']}/bravo.txt"), 403, REMOVE_FILE_DENIED),
        (lambda: alice.api.delete(f"/upload/{ub['id']}"), 403, REMOVE_DENIED),
        (lambda: alice.session.delete(f"/upload/{ub['id']}"), 403, REMOVE_DENIED),
        (lambda: plik.client().delete(f"/upload/{ub['id']}"), 403, REMOVE_DENIED),
        (lambda: alice.api.delete(f"/upload/{ub['id']}", headers={"X-UploadToken": "not-the-capability"}), 403, REMOVE_DENIED),
    ]
    for attempt, status, message in attempts:
        error(attempt(), status, message)
        assert plik.owner_state(bobby.id) == before
    assert plik.sql('SELECT count(*) FROM uploads WHERE "user" = %s', alice.id) == [(0,)]
    resp = bobby.api.get(f"/upload/{ub['id']}")
    boolean(resp.json()["admin"], True)
    ok(bobby.api.delete(f"/file/{ub['id']}/{fb['id']}/bravo.txt"))


def test_upload_capability_is_a_deliberate_exception(open_pair, plik):
    alice, bobby = open_pair
    ub, fb = bobby.upload("bravo.txt", b"bravo", removable=False)
    capability = {"X-UploadToken": ub["uploadToken"]}
    resp = plik.client().add_file(ub["id"], "guest.txt", b"from a guest", headers=capability)
    assert resp.status == 200, resp
    guest = resp.json()
    resp = alice.api.add_file(ub["id"], "alice.txt", b"alice with capability", headers=capability)
    assert resp.status == 200, resp
    via_alice = resp.json()
    # Files added with the capability belong to the upload's owner.
    assert plik.sql('SELECT "user", token FROM uploads WHERE id = %s', ub["id"]) == [(bobby.id, bobby.token)]
    assert plik.objects() == {
        plik.key(ub["id"], fb["id"]): b"bravo",
        plik.key(ub["id"], guest["id"]): b"from a guest",
        plik.key(ub["id"], via_alice["id"]): b"alice with capability",
    }
    counts(bobby.session.get("/me/stats").json(), {"uploads": 1, "files": 3, "totalSize": 5 + 12 + 21})
    counts(alice.session.get("/me/stats").json(), {"uploads": 0, "files": 0, "totalSize": 0})
    ids, body = listing_ids(bobby.session)
    assert ids == [ub["id"]]
    assert sorted(x["fileName"] for x in body["results"][0]["files"]) == ["alice.txt", "bravo.txt", "guest.txt"]
    assert listing_ids(alice.session)[0] == []
    body = plik.client().get(f"/upload/{ub['id']}", headers=capability).json()
    boolean(body["admin"], True)
    ok(plik.client().delete(f"/upload/{ub['id']}", headers=capability))
    assert plik.sql("SELECT deleted_at IS NOT NULL FROM uploads WHERE id = %s", ub["id"]) == [(True,)]


def test_removable_upload_can_be_deleted_by_anyone(open_pair, plik):
    alice, bobby = open_pair
    first, _ = bobby.upload("r1.txt", b"r1", removable=True)
    boolean(first["removable"], True)
    second, f2 = bobby.upload("r2.txt", b"r2", removable=True)
    keep, _ = bobby.upload("keep.txt", b"keep", removable=False)
    ok(alice.api.delete(f"/upload/{first['id']}"))
    ok(plik.client().delete(f"/file/{second['id']}/{f2['id']}/r2.txt"))
    ok(plik.client().delete(f"/upload/{second['id']}"))
    error(plik.client().delete(f"/upload/{keep['id']}"), 403, REMOVE_DENIED)
    assert sorted(plik.sql('SELECT id, removable, deleted_at IS NOT NULL FROM uploads WHERE "user" = %s', bobby.id)) == sorted(
        [(first["id"], True, True), (second["id"], True, True), (keep["id"], False, False)]
    )
    assert plik.sql("SELECT status FROM files WHERE id = %s", f2["id"]) == [("deleted",)]


def test_upload_management_follows_the_creating_token(open_pair, plik):
    alice, bobby = open_pair
    other = plik.client(bobby.new_token("second"))
    by_main, _ = bobby.upload("main.txt", b"main")
    by_other, _ = bobby.upload("other.txt", b"other", client=other)
    by_session, _ = bobby.upload("sess.txt", b"sess", client=bobby.session)
    # A different token of the same owner is not an administrator of the upload.
    boolean(other.get(f"/upload/{by_main['id']}").json()["admin"], False)
    before = plik.owner_state(bobby.id)
    error(other.add_file(by_main["id"], "x.txt", b"x"), 403, ADD_DENIED)
    assert plik.owner_state(bobby.id) == before
    error(other.delete(f"/upload/{by_main['id']}"), 403, REMOVE_DENIED)
    assert plik.owner_state(bobby.id) == before
    # Uploads made by session are not managed by a token, but the owner's session manages every own upload.
    error(bobby.api.add_file(by_session["id"], "x.txt", b"x"), 403, ADD_DENIED)
    assert plik.owner_state(bobby.id) == before
    for up in (by_main, by_other, by_session):
        boolean(bobby.session.get(f"/upload/{up['id']}").json()["admin"], True)
    assert bobby.session.add_file(by_other["id"], "more.txt", b"more").status == 200
    ok(bobby.session.delete(f"/upload/{by_main['id']}"))
    # Listing filters by creating token, only within the caller's own uploads.
    assert listing_ids(bobby.session)[0] == sorted([by_other["id"], by_session["id"]])
    assert listing_ids(bobby.session, f"?token={other.token}")[0] == [by_other["id"]]
    assert listing_ids(bobby.session, f"?token={bobby.token}")[0] == []
    assert listing_ids(alice.session, f"?token={other.token}")[0] == []
    error(bobby.session.get("/me/uploads?token=garbage"), 400, "invalid token format")
    counts(bobby.session.get(f"/me/stats?token={other.token}").json(), {"uploads": 1, "files": 2, "totalSize": 9})
    counts(bobby.session.get("/me/stats").json(), {"uploads": 2, "files": 3, "totalSize": 13})


def test_listing_pagination_stays_within_caller(open_pair, plik):
    alice, bobby = open_pair
    mine = [alice.create_upload(comments=f"a{i}")["id"] for i in range(3)]
    theirs = [bobby.create_upload(comments=f"b{i}")["id"] for i in range(2)]
    first = page(alice.session.get("/me/uploads?limit=2"))
    number(first["total"], 3)
    assert len(first["results"]) == 2 and first["after"], first
    assert [x["id"] for x in first["results"]] == [mine[2], mine[1]]
    second = page(alice.session.get(f"/me/uploads?limit=2&after={first['after']}"))
    assert [x["id"] for x in second["results"]] == [mine[0]]
    empty(second.get("after"))
    ascending = page(alice.session.get("/me/uploads?order=asc"))
    assert [x["id"] for x in ascending["results"]] == mine
    assert [x["id"] for x in page(bobby.session.get("/me/uploads?order=asc"))["results"]] == theirs
    error(alice.session.get("/me/uploads?order=sideways"), 400, "invalid order")
    error(alice.session.get("/me/uploads?limit=-3"), 400, "invalid limit")


def test_bulk_removal_is_scoped_to_caller(open_pair, plik):
    alice, bobby = open_pair
    a1, af = alice.upload("a1.txt", b"a1")
    a2 = alice.create_upload(alice.session)
    b1, bf = bobby.upload("b1.txt", b"b1")
    bobby_before = plik.owner_state(bobby.id)
    resp = alice.session.delete("/me/uploads")
    assert resp.status == 200 and resp.text.rstrip() == "2 uploads removed", resp
    assert sorted(plik.sql('SELECT id, deleted_at IS NOT NULL FROM uploads WHERE "user" = %s', alice.id)) == sorted(
        [(a1["id"], True), (a2["id"], True)]
    )
    assert plik.owner_state(bobby.id) == bobby_before
    assert listing_ids(alice.session)[0] == []
    assert listing_ids(bobby.session)[0] == [b1["id"]]
    assert plik.client().get(f"/file/{b1['id']}/{bf['id']}/b1.txt").body == b"b1"
    error(plik.client().get(f"/file/{a1['id']}/{af['id']}/a1.txt"), 404, f"upload {a1['id']} not found")
    # Token-filtered removal only touches uploads made with that token.
    spare = plik.client(bobby.new_token("spare"))
    b2, _ = bobby.upload("b2.txt", b"b2", client=spare)
    resp = bobby.session.delete(f"/me/uploads?token={spare.token}")
    assert resp.status == 200 and resp.text.rstrip() == "1 uploads removed", resp
    assert sorted(plik.sql('SELECT id, deleted_at IS NOT NULL FROM uploads WHERE "user" = %s', bobby.id)) == sorted(
        [(b1["id"], False), (b2["id"], True)]
    )
    error(bobby.session.delete("/me/uploads?token=junk"), 400, "invalid token format")
    resp = alice.session.delete(f"/me/uploads?token={bobby.token}")
    assert resp.status == 200 and resp.text.rstrip() == "0 uploads removed", resp
    assert plik.sql("SELECT deleted_at IS NOT NULL FROM uploads WHERE id = %s", b1["id"]) == [(False,)]


def test_session_cannot_revoke_another_users_token(open_pair, plik):
    alice, bobby = open_pair
    ub, _ = bobby.upload("b.txt", b"bobby data")
    error(alice.session.delete(f"/me/token/{bobby.token}"), 404, "token not found")
    assert plik.sql("SELECT user_id FROM tokens WHERE token = %s", bobby.token) == [(bobby.id,)]
    error(alice.session.delete("/me/token/plik_doesnotexist"), 404, "token not found")
    tokens = page(alice.session.get("/me/token"))["results"]
    assert [t["token"] for t in tokens] == [alice.token]
    # Bobby's token keeps working and keeps managing Bobby's upload.
    resp = bobby.api.get(f"/upload/{ub['id']}")
    assert resp.status == 200, resp
    boolean(resp.json()["admin"], True)
    up = bobby.create_upload()
    assert plik.sql('SELECT "user", token FROM uploads WHERE id = %s', up["id"]) == [(bobby.id, bobby.token)]
    # A foreign token is no listing key either.
    body = page(alice.session.get(f"/me/uploads?token={bobby.token}"))
    assert body["results"] == []
    number(body["total"], 0)
    empty(body.get("after"))
