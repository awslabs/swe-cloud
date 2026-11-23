"""Self-service profile and account deletion, and the CLI's other user fields (name, email, password, file size, TTL, admin)."""

import json

from plikenv import error, number, solo


def test_patch_me_updates_the_profile(plik):
    alice = solo(plik)
    resp = alice.session.request("PATCH", "/me", {"name": "Alice Example", "email": "alice@example.test"})
    assert resp.status == 200, resp
    body = alice.session.get("/me").json()
    assert body["name"] == "Alice Example" and body["email"] == "alice@example.test"
    assert plik.sql("SELECT name, email FROM users WHERE id = %s", alice.id) == [("Alice Example", "alice@example.test")]


def test_patch_me_without_a_body_is_refused(plik):
    alice = solo(plik)
    error(alice.session.request("PATCH", "/me"), 400, "missing request body")


def test_delete_me_removes_the_account(plik):
    alice = solo(plik)
    up, _ = alice.upload("mine.txt", b"mine")
    resp = alice.session.delete("/me")
    assert resp.status == 200, resp
    assert plik.sql("SELECT count(*) FROM users") == [(0,)]
    assert plik.sql("SELECT count(*) FROM tokens") == [(0,)]
    assert plik.sql("SELECT deleted_at IS NOT NULL FROM uploads WHERE id = %s", up["id"]) == [(True,)]


def test_cli_user_show_reports_profile_and_limits(plik):
    result = plik.cli(
        "user", "create", "--login", "carol", "--password", "carol-secret-3", "--name", "Carol C", "--email", "c@example.test",
        "--max-file-size", "2KB", "--max-ttl", "2h",
    )
    assert result.returncode == 0, result
    record = json.loads(plik.cli("user", "show", "--login", "carol").stdout)
    assert record["name"] == "Carol C" and record["email"] == "c@example.test"
    number(record["maxFileSize"], 2000)
    number(record["maxTTL"], 7200)
    assert plik.sql("SELECT max_file_size, max_ttl FROM users") == [(2000, 7200)]


def test_cli_password_change_takes_effect_on_login(plik):
    plik.add_user("alice", "alice-secret-1")
    result = plik.cli("user", "update", "--login", "alice", "--password", "alice-secret-NEW")
    assert result.returncode == 0, result
    plik.start()
    error(plik.client().login("alice", "alice-secret-1"), 403, "invalid credentials")
    assert plik.client().login("alice", "alice-secret-NEW").status == 200


def test_cli_admin_grant_by_update(plik):
    plik.add_user("alice", "alice-secret-1")
    result = plik.cli("user", "update", "--login", "alice", "--admin")
    assert result.returncode == 0, result
    assert plik.sql("SELECT is_admin FROM users") == [(True,)]
    assert json.loads(plik.cli("user", "show", "--login", "alice").stdout)["admin"] is True
