"""Invocation, configuration and offline provisioning through the program's own CLI."""

import json
import urllib.request

from harness import run_program
from plikenv import free_port, number


def users(plik):
    return plik.sql("SELECT id, login, provider, max_user_size FROM users ORDER BY id")


def test_user_create_records_local_users_with_limits(plik):
    for login, password, limit in (("alice", "alice-secret-1", "64B"), ("bobby", "bobby-secret-2", "1KB")):
        result = plik.cli("user", "create", "--login", login, "--password", password, "--max-user-size", limit)
        assert result.returncode == 0, result
    result = plik.cli("user", "create", "--login", "carol", "--password", "carol-secret-3", "--max-user-size", "-1")
    assert result.returncode == 0, result
    result = plik.cli("user", "create", "--login", "dave1", "--password", "dave-secret-4")
    assert result.returncode == 0, result
    assert users(plik) == [
        ("local:alice", "alice", "local", 64),
        ("local:bobby", "bobby", "local", 1000),
        ("local:carol", "carol", "local", -1),
        ("local:dave1", "dave1", "local", 0),
    ]
    listed = plik.cli("user", "list")
    assert listed.returncode == 0, listed
    assert sorted(listed.stdout.split()) == ["local:alice", "local:bobby", "local:carol", "local:dave1"]


def test_user_create_refusals_leave_users_unchanged(plik):
    plik.add_user("alice", "alice-secret-1", "64B")
    before = users(plik)
    duplicate = plik.cli("user", "create", "--login", "alice", "--password", "other-pass", "--max-user-size", "1KB")
    assert duplicate.returncode != 0, duplicate
    assert users(plik) == before
    short = plik.cli("user", "create", "--login", "bob", "--password", "bob-secret")
    assert short.returncode != 0, short
    assert users(plik) == before


def test_user_show_and_update(plik):
    plik.add_user("bobby", "bobby-secret-2", "1KB")
    shown = plik.cli("user", "show", "--login", "bobby")
    assert shown.returncode == 0, shown
    record = json.loads(shown.stdout)
    assert record["id"] == "local:bobby" and record["login"] == "bobby" and record["provider"] == "local"
    number(record["maxUserSize"], 1000)
    assert record["admin"] is False
    updated = plik.cli("user", "update", "--login", "bobby", "--max-user-size", "2KB")
    assert updated.returncode == 0, updated
    assert users(plik) == [("local:bobby", "bobby", "local", 2000)]
    missing = plik.cli("user", "update", "--login", "nobody", "--max-user-size", "2KB")
    assert missing.returncode != 0, missing
    assert plik.cli("user", "show", "--login", "nobody").returncode != 0
    assert users(plik) == [("local:bobby", "bobby", "local", 2000)]


def test_cli_token_lifecycle_is_usable_over_http(plik):
    plik.add_user("alice", "alice-secret-1")
    plik.add_user("bobby", "bobby-secret-2")
    created = plik.cli("token", "create", "--login", "bobby", "--comment", "cli tok")
    assert created.returncode == 0, created
    rows = plik.sql("SELECT token, coalesce(comment, '') FROM tokens WHERE user_id = 'local:bobby'")
    assert len(rows) == 1 and rows[0][1] == "cli tok", rows
    token = rows[0][0]
    assert token and token in created.stdout, created.stdout
    other = plik.cli("token", "create", "--login", "alice")
    assert other.returncode == 0, other
    rows = plik.sql("SELECT token, coalesce(comment, '') FROM tokens WHERE user_id = 'local:alice'")
    assert len(rows) == 1 and rows[0][1] == "", rows
    alice_token = rows[0][0]
    assert alice_token != token and alice_token in other.stdout, other.stdout
    assert plik.sql("SELECT count(*) FROM tokens") == [(2,)]
    listed = plik.cli("token", "list", "--login", "bobby")
    assert listed.returncode == 0, listed
    assert token in listed.stdout and alice_token not in listed.stdout
    assert plik.cli("token", "create", "--login", "nobody").returncode != 0

    plik.start()
    resp = plik.client(token).post("/upload", {})
    assert resp.status == 200, resp
    upload_id = resp.json()["id"]
    assert plik.sql('SELECT "user", token FROM uploads WHERE id = %s', upload_id) == [("local:bobby", token)]

    deleted = plik.cli("token", "delete", "--login", "bobby", "--token", token)
    assert deleted.returncode == 0, deleted
    assert plik.sql("SELECT user_id FROM tokens WHERE token = %s", token) == []
    assert plik.sql("SELECT user_id FROM tokens WHERE token = %s", alice_token) == [("local:alice",)]
    resp = plik.client(token).post("/upload", {})
    assert resp.status == 403, resp
    again = plik.cli("token", "delete", "--login", "bobby", "--token", token)
    assert again.returncode != 0, again


def test_health_config_and_port_override(plik):
    plik.write_config(max_user_size="1KB")
    plik.add_user("alice", "alice-secret-1")
    override = free_port()
    plik.start(["--port", override], port=override)
    with urllib.request.urlopen(f"http://127.0.0.1:{override}/health", timeout=5) as resp:
        assert resp.status == 200
    with urllib.request.urlopen(f"http://127.0.0.1:{override}/config", timeout=5) as resp:
        config = json.loads(resp.read())
    assert config["feature_authentication"] == "forced"
    assert config["feature_api_tokens"] == "enabled"
    assert config["feature_local_login"] == "enabled"
    assert config["feature_removable"] == "enabled"
    assert config["feature_password"] == "enabled"
    assert config["authentication"] is True and config["noAnonymousUploads"] is True
    number(config["maxUserSize"], 1000)
    # The configured ListenPort is not served when --port overrides it.
    try:
        urllib.request.urlopen(f"{plik.base}/health", timeout=2)
        served = True
    except Exception:
        served = False
    assert not served


def test_config_from_environment_variable(plik):
    plik.add_user("alice", "alice-secret-1")
    result = run_program(["user", "list"], cwd=plik.dir, env={"PLIKD_CONFIG": str(plik.config)})
    assert result.returncode == 0, result
    assert result.stdout.split() == ["local:alice"]

