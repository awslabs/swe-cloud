"""Invocation, configuration resolution, startup schema, health checks and shutdown."""

import http.client
import json
import time

from harness import run_program
from realtime import ALL_EVENTS, PG_PASS, PG_USER, free_port, same_multiset, wait_for

SCHEMA = {
    # column: (data_type, nullable, default)
    "id": ("character varying", "NO", None),
    "key": ("character varying", "NO", None),
    "secret": ("character varying", "NO", None),
    "max_connections": ("integer", "NO", None),
    "enable_client_messages": ("boolean", "NO", "false"),
    "enabled": ("boolean", "NO", "true"),
    "max_backend_events_per_second": ("integer", "YES", None),
    "max_client_events_per_second": ("integer", "NO", None),
    "max_read_requests_per_second": ("integer", "YES", None),
    "max_presence_members_per_channel": ("integer", "YES", None),
    "max_presence_member_size_in_kb": ("integer", "YES", None),
    "max_channel_name_length": ("integer", "YES", None),
    "max_event_channels_at_once": ("integer", "YES", None),
    "max_event_name_length": ("integer", "YES", None),
    "max_event_payload_in_kb": ("integer", "YES", None),
    "max_event_batch_size": ("integer", "YES", None),
    "enable_user_authentication": ("boolean", "YES", None),
    "enable_watchlist_events": ("boolean", "YES", None),
    "policy": ("jsonb", "YES", None),
    "webhooks": ("jsonb", "YES", None),
    "allowed_origins": ("jsonb", "YES", None),
    "created_at": ("timestamp without time zone", "YES", "CURRENT_TIMESTAMP"),
    "updated_at": ("timestamp without time zone", "YES", "CURRENT_TIMESTAMP"),
    "channel_delta_compression": ("jsonb", "YES", None),
    "idempotency": ("jsonb", "YES", None),
    "connection_recovery": ("jsonb", "YES", None),
}


def columns(conn, table):
    rows = conn.execute("SELECT column_name, data_type, is_nullable, column_default, character_maximum_length "
                        "FROM information_schema.columns WHERE table_schema = 'public' AND table_name = %s", (table,)).fetchall()
    return {name: (kind, nullable, default, length) for name, kind, nullable, default, length in rows}


def unique_columns(conn, table):
    rows = conn.execute("SELECT tc.constraint_type, kcu.column_name FROM information_schema.table_constraints tc "
                        "JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name "
                        "WHERE tc.table_name = %s AND tc.constraint_type IN ('PRIMARY KEY', 'UNIQUE')", (table,)).fetchall()
    return sorted(rows)


def up(server, path="/up"):
    r = server.up(path)
    return (r.status, r.body, r.headers.get("x-health-check")) if r else None


def test_command_line():
    r = run_program(["--help"], timeout=15)
    assert r.returncode == 0 and "--config" in r.stdout, r
    r = run_program(["--no-such-flag"], timeout=15)
    assert r.returncode == 2 and "--no-such-flag" in r.stderr, r


def test_startup_creates_the_application_table(deploy):
    d = deploy()
    s = d.start()
    assert up(s) == (503, b"ERROR", "ERROR")
    with d.conn() as c:
        cols = columns(c, "applications")
        assert {name: value[:3] for name, value in cols.items()} == SCHEMA, cols
        assert all(cols[name][3] == 255 for name in ("id", "key", "secret")), cols
        assert unique_columns(c, "applications") == [("PRIMARY KEY", "id"), ("UNIQUE", "key")]
        assert c.execute("SELECT count(*) FROM applications").fetchone() == (0,)
        tables = c.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'").fetchall()
        assert tables == [("applications",)], tables


def test_existing_table_is_extended_and_its_rows_are_kept(deploy):
    d = deploy()
    with d.conn() as c:
        c.execute('CREATE TABLE "tenants" (id VARCHAR(255) PRIMARY KEY, key VARCHAR(255) UNIQUE NOT NULL, secret VARCHAR(255) NOT NULL, '
                  "max_connections INTEGER NOT NULL, enable_client_messages BOOLEAN NOT NULL DEFAULT FALSE, enabled BOOLEAN NOT NULL DEFAULT TRUE, "
                  "max_backend_events_per_second INTEGER, max_client_events_per_second INTEGER NOT NULL, max_read_requests_per_second INTEGER, "
                  "max_presence_members_per_channel INTEGER, max_presence_member_size_in_kb INTEGER, max_channel_name_length INTEGER, "
                  "max_event_channels_at_once INTEGER, max_event_name_length INTEGER, max_event_payload_in_kb INTEGER, max_event_batch_size INTEGER, "
                  "enable_user_authentication BOOLEAN, enable_watchlist_events BOOLEAN, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
                  "updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        c.execute("INSERT INTO tenants (id, key, secret, max_connections, max_client_events_per_second) VALUES ('legacy', 'legacy-key', 'legacy-secret', 10, 10)")
        before = c.execute("SELECT id, key, secret, max_connections, enable_client_messages, enabled, created_at FROM tenants").fetchall()
    s = d.start(sections={"database.postgres": {"table_name": "tenants"}})
    with d.conn() as c:
        cols = columns(c, "tenants")
        assert {name: value[:3] for name, value in cols.items()} == SCHEMA, cols
        assert c.execute("SELECT id, key, secret, max_connections, enable_client_messages, enabled, created_at FROM tenants").fetchall() == before
        assert c.execute("SELECT webhooks, policy, allowed_origins FROM tenants").fetchall() == [(None, None, None)]
        assert c.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'").fetchall() == [("tenants",)]
    s.wait_healthy()
    assert up(s, "/up/legacy") == (200, b"OK", "OK")
    client = s.ws("legacy-key")
    assert client.handshake()["event"] == "pusher:connection_established"
    client.close()


def test_health_checks(deploy):
    d = deploy()
    s = d.start()
    assert up(s) == (503, b"ERROR", "ERROR")
    assert up(s, "/up/anything") == (404, b"NOT_FOUND", "NOT_FOUND")
    live = d.app("live")
    off = d.app("off", enabled=False)
    with d.conn() as c:
        rows = c.execute("SELECT id, key, secret, enabled, webhooks, policy FROM applications ORDER BY id").fetchall()
    s.wait_healthy()
    assert up(s) == (200, b"OK", "OK")
    assert up(s, f"/up/{live.id}") == (200, b"OK", "OK")
    assert up(s, f"/up/{off.id}") == (503, b"ERROR", "ERROR")
    assert up(s, "/up/unknown-app") == (404, b"NOT_FOUND", "NOT_FOUND")
    assert up(s, f"/up/{live.key}") == (404, b"NOT_FOUND", "NOT_FOUND")
    # The server only reads app rows.
    with d.conn() as c:
        assert c.execute("SELECT id, key, secret, enabled, webhooks, policy FROM applications ORDER BY id").fetchall() == rows


def listening(port):
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        conn.request("GET", "/up")
        return conn.getresponse().status
    except OSError:
        return None


def test_configuration_sources(deploy):
    d = deploy()
    # No arguments: config/config.toml in the working directory is used.
    port = free_port()
    s = d.start(args=[], port=port, files={"config/config.toml": d.config(port)})
    app = d.app("cfg")
    s.wait_healthy()
    assert up(s, f"/up/{app.id}") == (200, b"OK", "OK")
    s.stop()

    # --config replaces the working-directory file as a whole.
    cwd_port, explicit_port = free_port(), free_port()
    s = d.start(port=explicit_port, files={"config/config.toml": d.config(cwd_port)})
    s.wait_healthy()
    assert up(s, f"/up/{app.id}") == (200, b"OK", "OK")
    assert listening(cwd_port) is None
    s.stop()

    # A non-.toml file is read as JSON; ${NAME} placeholders and PORT apply on top of it.
    file_port, env_port = free_port(), free_port()
    cfg = {"host": "127.0.0.1", "port": file_port, "app_manager": {"driver": "pgsql"},
           "database": {"postgres": {"host": d.engines.pg[0], "port": d.engines.pg[1], "username": "${RT_DB_USER}",
                                     "password": "${RT_DB_PASSWORD}", "database": d.db}},
           "adapter": {"driver": "redis", "redis": {"prefix": d.adapter_prefix}},
           "cache": {"driver": "redis", "redis": {"prefix": d.cache_prefix}},
           "queue": {"driver": "sqs", "sqs": {"region": "us-east-1", "endpoint_url": "http://engines:4566", "queue_url_prefix": d.queue_prefix}},
           "metrics": {"enabled": False}, "rate_limiter": {"enabled": False}}
    s = d.start(args=["--config", "settings.json"], port=env_port, files={"settings.json": json.dumps(cfg)},
                env={"PORT": str(env_port), "RT_DB_USER": PG_USER, "RT_DB_PASSWORD": PG_PASS})
    s.wait_healthy()
    assert up(s, f"/up/{app.id}") == (200, b"OK", "OK")
    assert listening(file_port) is None
    s.stop()


def test_unparseable_config_file_keeps_the_earlier_configuration(deploy):
    d = deploy()
    good_port, bad_port = free_port(), free_port()
    broken = d.config(bad_port).replace('driver = "pgsql"', 'driver = "postgres"')
    s = d.start(args=["--config", "broken.toml"], port=good_port,
                files={"config/config.toml": d.config(good_port), "broken.toml": broken})
    app = d.app("kept")
    s.wait_healthy()
    assert up(s, f"/up/{app.id}") == (200, b"OK", "OK")
    assert listening(bad_port) is None
    s.stop()
    # Plain syntax errors in TOML or JSON behave the same way.
    for name, text in (("broken-syntax.toml", d.config(bad_port) + "\n[[[ not toml\n"), ("broken.json", "{not json")):
        s = d.start(args=["--config", name], port=good_port, files={"config/config.toml": d.config(good_port), name: text})
        s.wait_healthy()
        assert up(s, f"/up/{app.id}") == (200, b"OK", "OK")
        assert listening(bad_port) is None
        s.stop()


def test_shutdown_closes_connections_and_flushes_webhooks(deploy):
    d = deploy()
    s = d.start()
    app = d.app("bye", webhooks=[{"url": d.sink.url(), "event_types": ALL_EVENTS}])
    s.wait_healthy()
    c = s.connect(app)
    c.join("presence-s", app, "leaver")
    c.join("plain-s")
    assert wait_for(lambda: len(d.sink.events()) >= 3, 10)
    time.sleep(1)
    started = time.monotonic()
    s.process.terminate()
    assert c.wait_closed(15) == (4200, "Server shutting down")
    assert c.pending() == [{"event": "pusher:error", "data": {"code": 4200, "message": "Server shutting down"}}]
    assert s.process.wait(timeout=30) == 0
    assert time.monotonic() - started < 15
    time.sleep(1)
    assert same_multiset(d.sink.events(), [
        {"name": "channel_occupied", "channel": "presence-s"}, {"name": "channel_occupied", "channel": "plain-s"},
        {"name": "member_added", "channel": "presence-s", "user_id": "leaver"},
        {"name": "channel_vacated", "channel": "presence-s"}, {"name": "channel_vacated", "channel": "plain-s"},
        {"name": "member_removed", "channel": "presence-s", "user_id": "leaver"}]), d.sink.events()
    assert d.queue_counts() == (0, 0)
