"""Invocation level: flags, installation/upgrade effects on the database, configuration and API authentication."""

import base64
import tomllib

from harness import run_command, run_program, workspace, write_fixture
from mailer import ADMIN_ENV, TOKEN_PREFIX, App, assert_json, config_text, parse_token

SEED_LISTS = [
    ("Default list", "private", "single", "active", ["test"], ""),
    ("Opt-in list", "public", "double", "active", ["test"], ""),
]
SEED_SUBSCRIBERS = [
    ("john@example.com", "John Doe", "enabled", {"city": "Bengaluru", "good": True, "type": "known"}),
    ("anon@example.com", "Anon Doe", "enabled", {"city": "Bengaluru", "good": True, "type": "unknown"}),
]
GRADED_TABLES = {"lists", "subscribers", "subscriber_lists", "bounces", "media", "settings"}


def list_rows(db):
    return db.q("SELECT name, type::text, optin::text, status::text, COALESCE(tags, '{}'), description FROM lists ORDER BY id")


def add_marker(db):
    db.conn.execute("UPDATE lists SET name = 'Marker' WHERE name = 'Default list'")


def candidate_cat(path):
    result = run_command(["cat", str(path)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_version_flag():
    with workspace() as root:
        result = run_program(["--version"], cwd=root)
    assert result.returncode == 0, result.stderr
    assert "v6.2.0" in result.stdout


def test_new_config_writes_sample_once():
    with workspace() as root:
        result = run_program(["--new-config", "--config", "fresh.toml"], cwd=root)
        assert result.returncode == 0, (result.stdout, result.stderr)
        text = candidate_cat(root / "fresh.toml")
        parsed = tomllib.loads(text)
        assert isinstance(parsed["app"]["address"], str) and ":" in parsed["app"]["address"]
        for key in ("host", "user", "password", "database"):
            assert isinstance(parsed["db"][key], str), key
        assert type(parsed["db"]["port"]) is int
        # An existing file is never overwritten.
        again = run_program(["--new-config", "--config", "fresh.toml"], cwd=root)
        assert again.returncode != 0
        assert candidate_cat(root / "fresh.toml") == text


def test_missing_config_file_is_fatal():
    with workspace() as root:
        result = run_program(["--install", "--yes"], cwd=root)
    assert result.returncode != 0


def test_install_creates_schema_and_seed_data(database):
    assert not GRADED_TABLES & database.tables()
    result = database.install()
    assert GRADED_TABLES <= database.tables()
    token_lines = [line for line in result.stderr.splitlines() if line.startswith(TOKEN_PREFIX)]
    assert len(token_lines) == 1
    assert len(parse_token(result.stderr)) >= 8
    assert TOKEN_PREFIX not in result.stdout

    assert list_rows(database) == SEED_LISTS
    subscribers = database.q("SELECT email, name, status::text, attribs FROM subscribers ORDER BY id")
    assert subscribers == SEED_SUBSCRIBERS
    memberships = database.q(
        "SELECT s.email, l.name, sl.status::text FROM subscriber_lists sl JOIN subscribers s ON s.id = sl.subscriber_id "
        "JOIN lists l ON l.id = sl.list_id ORDER BY 1")
    assert memberships == [("anon@example.com", "Opt-in list", "unconfirmed"), ("john@example.com", "Default list", "unconfirmed")]
    settings = dict(database.q("SELECT key, value FROM settings WHERE key IN ('upload.provider', 'bounce.enabled', 'bounce.webhooks_enabled', 'bounce.actions', 'privacy.domain_blocklist')"))
    assert_json(settings, {
        "upload.provider": "filesystem",
        "bounce.enabled": False,
        "bounce.webhooks_enabled": False,
        "bounce.actions": {"soft": {"count": 2, "action": "none"}, "hard": {"count": 1, "action": "blocklist"}, "complaint": {"count": 1, "action": "blocklist"}},
        "privacy.domain_blocklist": [],
    })
    assert database.q("SELECT COUNT(*) FROM bounces") == [(0,)]
    assert database.q("SELECT COUNT(*) FROM media") == [(0,)]


def test_install_without_admin_variables_prints_no_token(database):
    result = database.run("--install", "--yes", "--config", "config.toml")
    assert result.returncode == 0, result.stdout[-2000:]
    assert TOKEN_PREFIX not in result.stderr and TOKEN_PREFIX not in result.stdout
    assert list_rows(database) == SEED_LISTS


def test_install_rejects_short_admin_credentials(database):
    result = database.run("--install", "--yes", "--config", "config.toml",
                          env={"LISTMONK_ADMIN_USER": "ad", "LISTMONK_ADMIN_PASSWORD": "short"})
    assert result.returncode != 0
    assert TOKEN_PREFIX not in result.stderr


def test_reinstall_modes(database):
    database.install()
    add_marker(database)
    marked = [("Marker", *SEED_LISTS[0][1:]), SEED_LISTS[1]]

    # Without --yes the confirmation reads end-of-input and nothing changes.
    refused = database.run("--install", "--config", "config.toml", env=ADMIN_ENV)
    assert refused.returncode != 0
    assert list_rows(database) == marked

    idempotent = database.run("--install", "--yes", "--idempotent", "--config", "config.toml", env=ADMIN_ENV)
    assert idempotent.returncode == 0
    assert TOKEN_PREFIX not in idempotent.stderr
    assert list_rows(database) == marked

    upgrade = database.run("--upgrade", "--yes", "--config", "config.toml")
    assert upgrade.returncode == 0
    assert list_rows(database) == marked

    # A plain --install wipes and seeds again.
    database.install()
    assert list_rows(database) == SEED_LISTS


def test_idempotent_install_on_empty_database_installs(database):
    result = database.run("--install", "--yes", "--idempotent", "--config", "config.toml", env=ADMIN_ENV)
    assert result.returncode == 0
    assert GRADED_TABLES <= database.tables()
    assert list_rows(database) == SEED_LISTS
    parse_token(result.stderr)


def test_uninstalled_database_is_refused(database):
    upgrade = database.run("--upgrade", "--yes", "--config", "config.toml")
    assert upgrade.returncode != 0
    serve = database.run("--config", "config.toml", timeout=30)
    assert serve.returncode != 0
    assert not GRADED_TABLES & database.tables()


def test_environment_overrides_config_file(engine, database):
    with workspace() as root:
        bogus = config_text(engine, "nonexistent", database.port).replace(f'host = "{engine.host}"', 'host = "db.invalid"')
        write_fixture(root / "config.toml", bogus.replace(f"port = {engine.port}", "port = 1"), mode=0o644)
        env = {**ADMIN_ENV, "LISTMONK_db__host": engine.host, "LISTMONK_db__port": str(engine.port), "LISTMONK_db__database": database.name}
        result = run_program(["--install", "--yes"], cwd=root, env=env, timeout=60)
    assert result.returncode == 0, result.stdout[-2000:]
    assert list_rows(database) == SEED_LISTS


def test_api_requires_credentials(database):
    result = database.install()
    app = App(database, parse_token(result.stderr))
    try:
        for path in ("/api/health", "/api/lists", "/api/subscribers", "/api/settings"):
            status, body = app.call("GET", path, auth=False)
            assert status == 403, (path, status)
            assert isinstance(body, dict) and isinstance(body.get("message"), str), body
        assert app.call("GET", "/api/lists", auth="token apibot:not-the-token")[0] == 403
        wrong_user = "Basic " + base64.b64encode(f"admin:{app.token}".encode()).decode()
        assert app.call("GET", "/api/lists", auth=wrong_user)[0] == 403
        assert app.call("POST", "/api/lists", {"name": "Sneaky"}, auth=False)[0] == 403
        assert database.q("SELECT COUNT(*) FROM lists WHERE name = 'Sneaky'") == [(0,)]

        # Both credential forms are accepted.
        status, body = app.call("GET", "/api/lists?per_page=all", auth=f"token apibot:{app.token}")
        assert status == 200
        assert sorted(item["name"] for item in body["data"]["results"]) == ["Default list", "Opt-in list"]
        assert app.call("GET", "/api/health")[1] == {"data": True}
    finally:
        app.stop()
