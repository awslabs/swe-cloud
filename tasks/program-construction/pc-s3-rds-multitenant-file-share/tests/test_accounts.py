"""Password sessions, XSRF protection and API-token management over HTTP."""

from plikenv import boolean, error, number, ok, page


def tokens_of(plik, user_id):
    return sorted(row[0] for row in plik.sql("SELECT token FROM tokens WHERE user_id = %s", user_id))


def test_login_sets_session_and_identifies_each_user(plik):
    plik.add_user("alice", "alice-secret-1", "64B")
    plik.add_user("bobby", "bobby-secret-2", "1KB")
    plik.start()
    for login, password, limit in (("alice", "alice-secret-1", 64), ("bobby", "bobby-secret-2", 1000)):
        client = plik.client()
        resp = client.login(login, password)
        ok(resp)
        assert client.cookie("plik-session") and client.cookie("plik-xsrf")
        me = client.get("/me")
        assert me.status == 200, me
        body = me.json()
        assert body["id"] == f"local:{login}" and body["login"] == login and body["provider"] == "local", body
        boolean(body["admin"], False)
        number(body["maxUserSize"], limit)


def test_login_failures_set_no_session(plik):
    plik.add_user("alice", "alice-secret-1")
    plik.start()
    for login, password in (("alice", "wrong-password"), ("nobody", "alice-secret-1")):
        client = plik.client()
        error(client.login(login, password), 403, "invalid credentials")
        assert client.cookie("plik-session") is None
        error(client.get("/me"), 401, "you must be authenticated, please login first")
    client = plik.client()
    error(client.post("/auth/local/login", {"login": "alice"}), 400, "missing password")
    assert client.cookie("plik-session") is None


def test_session_mutations_require_matching_xsrf_header(plik):
    plik.add_user("alice", "alice-secret-1")
    plik.start()
    client = plik.client()
    ok(client.login("alice", "alice-secret-1"))
    error(client.post("/me/token", {"comment": "no header"}, xsrf=False), 403, "missing xsrf header")
    assert plik.sql("SELECT count(*) FROM tokens") == [(0,)]
    client = plik.client()
    ok(client.login("alice", "alice-secret-1"))
    error(client.post("/me/token", {"comment": "bad"}, headers={"X-XSRFToken": "not-the-cookie"}), 403, "invalid xsrf header")
    assert plik.sql("SELECT count(*) FROM tokens") == [(0,)]
    # GET requests need no header and a fresh session can mint tokens with it.
    client = plik.client()
    ok(client.login("alice", "alice-secret-1"))
    assert client.get("/me", xsrf=False).status == 200
    assert client.post("/me/token", {"comment": "good"}).status == 200
    assert plik.sql("SELECT user_id, comment FROM tokens") == [("local:alice", "good")]


def test_token_creation_and_listing_are_per_user(plik):
    plik.add_user("alice", "alice-secret-1")
    plik.add_user("bobby", "bobby-secret-2")
    plik.start()
    alice, bobby = plik.client(), plik.client()
    ok(alice.login("alice", "alice-secret-1"))
    ok(bobby.login("bobby", "bobby-secret-2"))
    first = alice.post("/me/token", {"comment": "alice one"})
    assert first.status == 200, first
    body = first.json()
    assert isinstance(body["token"], str) and body["token"] and body["comment"] == "alice one" and body["createdAt"], body
    second = alice.post("/me/token")
    assert second.status == 200, second
    assert second.json().get("comment") in (None, ""), second
    third = bobby.post("/me/token", {"comment": "bobby one"})
    a1, a2, b1 = first.json()["token"], second.json()["token"], third.json()["token"]
    assert len({a1, a2, b1}) == 3
    assert tokens_of(plik, "local:alice") == sorted([a1, a2])
    assert tokens_of(plik, "local:bobby") == [b1]

    listing = page(alice.get("/me/token"))
    assert sorted(t["token"] for t in listing["results"]) == sorted([a1, a2])
    assert {t["token"]: t.get("comment") or "" for t in listing["results"]} == {a1: "alice one", a2: ""}
    listing = page(bobby.get("/me/token"))
    assert [t["token"] for t in listing["results"]] == [b1]


def test_account_endpoints_do_not_accept_api_tokens(plik):
    plik.add_user("alice", "alice-secret-1")
    plik.start()
    tenant = plik.tenant("alice", "alice-secret-1")
    for path in ("/me", "/me/uploads", "/me/token", "/me/stats"):
        error(tenant.api.get(path), 401, "you must be authenticated, please login first")
    error(tenant.api.post("/me/token", {"comment": "via token"}), 401, "you must be authenticated, please login first")
    assert tokens_of(plik, "local:alice") == [tenant.token]


def test_revoking_own_token_disables_it_but_keeps_upload_links(plik):
    plik.add_user("alice", "alice-secret-1")
    plik.start()
    alice = plik.tenant("alice", "alice-secret-1")
    spare = alice.new_token("spare")
    up, _ = alice.upload("kept.txt", b"kept bytes\n")
    ok(alice.session.delete(f"/me/token/{alice.token}"))
    assert tokens_of(plik, "local:alice") == [spare]
    error(alice.api.post("/upload", {}), 403, "invalid token")
    error(alice.api.get(f"/upload/{up['id']}"), 403, "invalid token")
    assert plik.sql('SELECT "user", token FROM uploads WHERE id = %s', up["id"]) == [("local:alice", alice.token)]
    # The owner's session still manages the upload; the spare token never did.
    resp = alice.session.get(f"/upload/{up['id']}")
    assert resp.status == 200, resp
    boolean(resp.json()["admin"], True)
    resp = plik.client(spare).get(f"/upload/{up['id']}")
    boolean(resp.json()["admin"], False)
    error(alice.session.delete(f"/me/token/{alice.token}"), 404, "token not found")


def test_logout_ends_the_client_session(plik):
    plik.add_user("alice", "alice-secret-1")
    plik.start()
    client = plik.client()
    ok(client.login("alice", "alice-secret-1"))
    assert client.get("/auth/logout").status == 200
    assert client.cookie("plik-session") in (None, "")
    error(client.get("/me"), 401, "you must be authenticated, please login first")
