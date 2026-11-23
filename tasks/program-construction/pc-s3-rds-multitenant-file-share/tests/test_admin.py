"""Administrator accounts: the admin HTTP API, admin rights over other users' uploads, and impersonation."""

import pytest

from plikenv import counts, error, number, page


@pytest.fixture
def admin_env(plik):
    """Server with an administrator (created with --admin) and two regular users with uploads."""
    result = plik.cli("user", "create", "--login", "root1", "--password", "root-secret-1", "--admin")
    assert result.returncode == 0, result
    plik.add_user("alice", "alice-secret-1", "-1")
    plik.add_user("bobby", "bobby-secret-2", "1KB")
    plik.start()
    admin = plik.tenant("root1", "root-secret-1")
    alice = plik.tenant("alice", "alice-secret-1")
    return admin, alice


def test_cli_admin_flag_creates_an_administrator(plik):
    result = plik.cli("user", "create", "--login", "root1", "--password", "root-secret-1", "--admin")
    assert result.returncode == 0, result
    plik.add_user("alice", "alice-secret-1")
    assert sorted(plik.sql("SELECT id, is_admin FROM users")) == [("local:alice", False), ("local:root1", True)]


def test_admin_lists_all_users(admin_env):
    admin, _ = admin_env
    body = page(admin.session.get("/users"))
    number(body["total"], 3)
    assert sorted(u["id"] for u in body["results"]) == ["local:alice", "local:bobby", "local:root1"]


def test_admin_user_listing_filters_administrators(admin_env):
    admin, _ = admin_env
    body = page(admin.session.get("/users?admin=true"))
    assert [u["id"] for u in body["results"]] == ["local:root1"]


def test_regular_user_is_refused_admin_endpoints(admin_env, plik):
    _, alice = admin_env
    for path in ("/users", "/stats", "/uploads"):
        error(alice.session.get(path), 403, "you need administrator privileges")


def test_admin_reads_server_statistics(admin_env):
    admin, alice = admin_env
    alice.upload("a.txt", b"a" * 30)
    alice.upload("b.txt", b"b" * 12)
    counts(
        admin.session.get("/stats").json(),
        {"users": 3, "uploads": 2, "anonymousUploads": 0, "files": 2, "totalSize": 42, "anonymousTotalSize": 0},
    )


def test_admin_lists_uploads_of_one_user(admin_env, plik):
    admin, alice = admin_env
    mine = admin.create_upload()
    a1, _ = alice.upload("a.txt", b"aaa")
    body = page(admin.session.get("/uploads?user=local:alice"))
    assert [u["id"] for u in body["results"]] == [a1["id"]]
    body = page(admin.session.get("/uploads"))
    assert sorted(u["id"] for u in body["results"]) == sorted([mine["id"], a1["id"]])


def test_admin_reads_any_user(admin_env):
    admin, _ = admin_env
    body = admin.session.get("/user/local:bobby").json()
    assert body["id"] == "local:bobby" and body["login"] == "bobby"
    number(body["maxUserSize"], 1000)


def test_user_cannot_read_another_user(admin_env):
    _, alice = admin_env
    error(alice.session.get("/user/local:bobby"), 403, "you need administrator privileges")


def test_admin_creates_a_local_user_over_http(admin_env, plik):
    admin, _ = admin_env
    resp = admin.session.post("/user", {"login": "carol", "password": "carol-secret-3", "provider": "local", "maxUserSize": 2000})
    assert resp.status == 200, resp
    assert resp.json()["id"] == "local:carol"
    assert plik.sql("SELECT login, provider, max_user_size FROM users WHERE id = 'local:carol'") == [("carol", "local", 2000)]
    assert plik.client().login("carol", "carol-secret-3").status == 200


def test_admin_updates_user_limits_over_http(admin_env, plik):
    admin, _ = admin_env
    resp = admin.session.post(
        "/user/local:bobby", {"id": "local:bobby", "login": "bobby", "provider": "local", "maxUserSize": 500, "maxTTL": 99}
    )
    assert resp.status == 200, resp
    assert plik.sql("SELECT max_user_size, max_ttl, is_admin FROM users WHERE id = 'local:bobby'") == [(500, 99, False)]


def test_user_cannot_change_own_quota(admin_env, plik):
    _, alice = admin_env
    resp = alice.session.post("/user/local:alice", {"id": "local:alice", "login": "alice", "provider": "local", "maxUserSize": 5})
    error(resp, 403, "can't edit your own quota, nice try!")
    assert plik.sql("SELECT max_user_size FROM users WHERE id = 'local:alice'") == [(-1,)]


def test_user_cannot_grant_self_admin(admin_env, plik):
    _, alice = admin_env
    resp = alice.session.post("/user/local:alice", {"id": "local:alice", "login": "alice", "provider": "local", "admin": True})
    error(resp, 403, "can't grant yourself admin right, nice try!")
    assert plik.sql("SELECT is_admin FROM users WHERE id = 'local:alice'") == [(False,)]


def test_admin_deletes_a_user_over_http(admin_env, plik):
    admin, alice = admin_env
    up, _ = alice.upload("a.txt", b"aaa")
    assert admin.session.delete("/user/local:alice").status == 200
    assert plik.sql("SELECT count(*) FROM users WHERE id = 'local:alice'") == [(0,)]
    assert plik.sql("SELECT deleted_at IS NOT NULL FROM uploads WHERE id = %s", up["id"]) == [(True,)]
    assert plik.client().login("alice", "alice-secret-1").status == 403


def test_admin_session_manages_another_users_upload(admin_env, plik):
    admin, alice = admin_env
    up, f = alice.upload("a.txt", b"aaa")
    body = admin.session.get(f"/upload/{up['id']}").json()
    assert body["admin"] is True and body["uploadToken"] == up["uploadToken"]
    assert admin.session.delete(f"/upload/{up['id']}").status == 200
    assert plik.sql("SELECT deleted_at IS NOT NULL FROM uploads WHERE id = %s", up["id"]) == [(True,)]


def test_admin_api_token_does_not_manage_another_users_upload(admin_env, plik):
    admin, alice = admin_env
    up, _ = alice.upload("a.txt", b"aaa")
    assert admin.api.get(f"/upload/{up['id']}").json()["admin"] is False
    error(admin.api.delete(f"/upload/{up['id']}"), 403, "you are not allowed to remove this upload")
    assert plik.sql("SELECT deleted_at FROM uploads WHERE id = %s", up["id"]) == [(None,)]


def test_admin_impersonates_another_user(admin_env):
    admin, alice = admin_env
    up, _ = alice.upload("a.txt", b"aaa")
    as_alice = {"X-Plik-Impersonate": "local:alice"}
    assert admin.session.get("/me", headers=as_alice).json()["id"] == "local:alice"
    body = page(admin.session.get("/me/uploads", headers=as_alice))
    assert [u["id"] for u in body["results"]] == [up["id"]]


def test_impersonated_upload_is_recorded_for_the_target_user(admin_env, plik):
    admin, _ = admin_env
    resp = admin.session.post("/upload", {}, headers={"X-Plik-Impersonate": "local:alice"})
    assert resp.status == 200, resp
    assert plik.sql('SELECT "user" FROM uploads WHERE id = %s', resp.json()["id"]) == [("local:alice",)]


def test_impersonation_requires_an_administrator(admin_env):
    _, alice = admin_env
    error(alice.session.get("/me", headers={"X-Plik-Impersonate": "local:root1"}), 403, "you need administrator privileges")


def test_impersonating_an_unknown_user_is_refused(admin_env):
    admin, _ = admin_env
    error(admin.session.get("/me", headers={"X-Plik-Impersonate": "local:nobody"}), 403, "user to impersonate does not exists")
