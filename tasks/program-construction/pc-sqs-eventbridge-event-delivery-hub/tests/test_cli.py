"""Command-line surface: version, migrations and configuration inspection."""

import re
import time
import urllib.request

import pytest

from hub import parse_time, uid

SQL_MIGRATIONS = [
    "sql/000001 init",
    "sql/000002 delivery_response",
    "sql/000003 event_delivery_index",
    "sql/000004 delivery_manual_attempt",
    "sql/000005 denormalize_attempts",
    "sql/000006 data_jsonb_to_text",
    "sql/000007 matched_destination_ids",
    "sql/000008 attempt_destination_type",
    "sql/000009 response_data_jsonb_to_text",
    "sql/000010 rename_attempts_pkey",
    "sql/000011 attempt_latency",
]


def lines(text):
    """Output lines with runs of whitespace collapsed; blank lines dropped."""
    return [" ".join(line.split()) for line in text.splitlines() if line.strip()]


def test_version_and_help(make_hub):
    hub = make_hub(start=False)
    version = hub.cli("--version")
    assert version.returncode == 0, version.stderr
    assert lines(version.stdout) == ["outpost version 224bff2"]
    helped = hub.cli("--help")
    assert helped.returncode == 0, helped.stderr
    commands = {line.split()[0].rstrip(",") for line in lines(helped.stdout.split("COMMANDS:", 1)[1].split("GLOBAL OPTIONS:", 1)[0])}
    assert {"serve", "migrate", "config"} <= commands


def test_subcommand_help_and_usage_errors(make_hub):
    hub = make_hub(start=False)
    migrate = hub.cli("migrate")
    assert migrate.returncode == 0
    names = [line.split()[0] for line in lines(migrate.stdout.split("COMMANDS:", 1)[1].split("OPTIONS:", 1)[0])]
    assert names == ["list", "plan", "apply", "verify", "unlock"]
    bad = hub.cli("migrate", "plan", "--bogus")
    assert bad.returncode != 0
    assert "flag provided but not defined: -bogus" in bad.stderr


def test_stale_lock_blocks_redis_migrations_until_unlocked(make_hub):
    hub = make_hub(start=False)
    declined = hub.cli("migrate", "unlock")
    assert declined.returncode == 0
    assert lines(declined.stdout)[-1] == "Continue? [y/N]: Cancelled."
    kv = hub.engines.valkey()
    lock = hub.deployment + ":.outpost:migration_lock"
    kv.set(lock, "stale-holder", ex=600)
    blocked = hub.cli("migrate", "apply", "--yes")
    assert blocked.returncode != 0
    assert "lock already held" in blocked.stderr
    assert kv.get(lock) == b"stale-holder"
    plan = lines(hub.cli("migrate", "plan").stdout)
    assert "Redis Migrations (2 pending):" in plan, plan
    cleared = hub.cli("migrate", "unlock", "--yes")
    assert cleared.returncode == 0, cleared.stderr
    assert lines(cleared.stdout) == ["Redis migration lock cleared."]
    assert kv.exists(lock) == 0
    hub.migrate()
    assert lines(hub.cli("migrate", "plan").stdout) == ["All migrations are up to date."]
    assert kv.exists(lock) == 0


def test_plan_list_and_verify_on_fresh_stores(make_hub):
    hub = make_hub(start=False)
    plan = hub.cli("migrate", "plan")
    assert plan.returncode == 0, plan.stderr
    out = lines(plan.stdout)
    sql_header = out.index("SQL Migrations (11 pending, v0 → v11):")
    assert out[sql_header + 1:sql_header + 12] == ["- " + m for m in SQL_MIGRATIONS]
    redis_header = out.index("Redis Migrations (2 pending):")
    redis_items = [line for line in out[redis_header + 1:] if line.startswith("- redis/")]
    assert [item.split(":")[0] for item in redis_items] == ["- redis/002_timestamps", "- redis/003_entity"]

    listing = hub.cli("migrate", "list")
    assert listing.returncode == 0, listing.stderr
    out = lines(listing.stdout)
    for name in SQL_MIGRATIONS:
        assert f"[pending ] {name}" in out
    assert any(line.startswith("[not_applicable] redis/001_hash_tags") for line in out)
    assert "reason: Not needed - using DEPLOYMENT_ID" in out
    assert any(line.startswith("[pending ] redis/002_timestamps") for line in out)
    assert any(line.startswith("[pending ] redis/003_entity") for line in out)
    assert out[-1] == "Summary: 11 SQL pending, 2 Redis pending"

    verify = hub.cli("migrate", "verify")
    assert verify.returncode != 0
    assert "SQL: current=0 latest=11 [BEHIND]" in lines(verify.stdout)
    assert "verification failed" in verify.stderr


def test_serve_refuses_pending_migrations(make_hub):
    hub = make_hub(start=False)
    result = hub.cli("serve", timeout=30)
    assert result.returncode != 0
    assert "pending migrations detected (11 SQL, 2 Redis)" in result.stderr
    with pytest.raises(OSError):
        urllib.request.urlopen(f"http://127.0.0.1:{hub.port}/healthz", timeout=1)
    with hub.pg() as conn:
        tables = {r[0] for r in conn.execute("select table_name from information_schema.tables where table_schema='public'")}
    assert not tables & {"events", "attempts", "events_default", "attempts_default"}


def test_apply_requires_confirmation(make_hub):
    hub = make_hub(start=False)
    declined = hub.cli("migrate", "apply")
    assert declined.returncode == 0, declined.stderr
    assert "Apply these migrations? [y/N]: Cancelled." in lines(declined.stdout)
    assert "SQL Migrations (11 pending, v0 → v11):" in lines(hub.cli("migrate", "plan").stdout)
    with hub.pg() as conn:
        tables = {r[0] for r in conn.execute("select table_name from information_schema.tables where table_schema='public'")}
    assert not tables & {"events", "attempts", "events_default", "attempts_default"}
    assert list(hub.engines.valkey().scan_iter(match=hub.deployment + ":*")) == []

    confirmed = hub.cli("migrate", "apply", stdin="y\n")
    assert confirmed.returncode == 0, (confirmed.stdout, confirmed.stderr[-2000:])
    assert "All migrations applied successfully." in lines(confirmed.stdout)
    assert lines(hub.cli("migrate", "plan").stdout) == ["All migrations are up to date."]


def test_apply_creates_schema_and_records(make_hub):
    hub = make_hub(start=False)
    applied = hub.cli("migrate", "apply", "--yes")
    assert applied.returncode == 0, applied.stderr
    assert "All migrations applied successfully." in lines(applied.stdout)

    again = hub.cli("migrate", "apply", "--yes")
    assert again.returncode == 0
    assert lines(again.stdout) == ["All migrations are up to date."]
    assert lines(hub.cli("migrate", "plan").stdout) == ["All migrations are up to date."]

    out = lines(hub.cli("migrate", "list").stdout)
    for name in SQL_MIGRATIONS:
        assert f"[applied ] {name}" in out
    assert any(line.startswith("[applied ] redis/002_timestamps") for line in out)
    assert any(line.startswith("[applied ] redis/003_entity") for line in out)
    assert out[-1] == "Summary: 0 SQL pending, 0 Redis pending"

    verify = hub.cli("migrate", "verify")
    assert verify.returncode == 0, verify.stdout + verify.stderr
    out = lines(verify.stdout)
    assert "SQL: current=11 latest=11 [OK]" in out
    assert "All migrations verified successfully." in out

    with hub.pg() as conn:
        tables = {r[0] for r in conn.execute("select table_name from information_schema.tables where table_schema='public'")}
        assert tables == {"events", "events_default", "attempts", "attempts_default", "schema_migrations"}
        assert conn.execute("select version, dirty from schema_migrations").fetchall() == [(11, False)]
        columns = {
            table: {r[0]: (r[1], r[2]) for r in conn.execute(
                "select column_name, data_type, is_nullable from information_schema.columns where table_name=%s", (table,))}
            for table in ("events", "attempts")
        }
    assert columns["events"] == {
        "id": ("text", "NO"), "tenant_id": ("text", "NO"), "time": ("timestamp with time zone", "NO"),
        "topic": ("text", "NO"), "eligible_for_retry": ("boolean", "NO"), "data": ("text", "NO"),
        "metadata": ("jsonb", "NO"), "matched_destination_ids": ("ARRAY", "NO"),
    }
    assert columns["attempts"] == {
        "id": ("text", "NO"), "event_id": ("text", "NO"), "destination_id": ("text", "NO"), "status": ("text", "NO"),
        "time": ("timestamp with time zone", "NO"), "code": ("text", "YES"), "response_data": ("text", "YES"),
        "manual": ("boolean", "NO"), "attempt_number": ("integer", "NO"), "tenant_id": ("text", "NO"),
        "topic": ("text", "NO"), "event_time": ("timestamp with time zone", "NO"), "eligible_for_retry": ("boolean", "NO"),
        "event_data": ("text", "NO"), "event_metadata": ("jsonb", "NO"), "destination_type": ("text", "NO"),
        "latency_ms": ("integer", "YES"),
    }

    kv = hub.engines.valkey()
    keys = sorted(k.decode() for k in kv.scan_iter(match=hub.deployment + ":*"))
    prefix = hub.deployment + ":outpost:migration:"
    assert keys == [prefix + "001_hash_tags", prefix + "002_timestamps", prefix + "003_entity"]
    skipped = {k.decode(): v.decode() for k, v in kv.hgetall(prefix + "001_hash_tags").items()}
    assert skipped.keys() == {"status", "checked_at", "reason"}
    assert skipped["status"] == "not_applicable" and skipped["reason"] == "Not needed - using DEPLOYMENT_ID"
    for name in ("002_timestamps", "003_entity"):
        record = {k.decode(): v.decode() for k, v in kv.hgetall(prefix + name).items()}
        assert record.keys() == {"status", "applied_at"} and record["status"] == "applied"
        parse_time(record["applied_at"])


def test_second_deployment_shares_sql_schema(make_hub):
    first = make_hub(start=False)
    first.migrate()
    second = make_hub(start=False, POSTGRES_URL=first.env["POSTGRES_URL"])
    plan = lines(second.cli("migrate", "plan").stdout)
    assert "SQL Migrations: up to date" in plan
    assert "Redis Migrations (2 pending):" in plan
    second.migrate()
    assert lines(second.cli("migrate", "plan").stdout) == ["All migrations are up to date."]
    second.start(migrate=False)
    assert second.request("GET", "/healthz", base="").status == 200


def test_config_list_reports_resolved_settings(make_hub):
    hub = make_hub(start=False, RETRY_SCHEDULE="5,10", MAX_DESTINATIONS_PER_TENANT=7, PUBLISH_MAX_REDELIVERIES=3)
    result = hub.cli("config", "list")
    assert result.returncode == 0, result.stderr
    settings = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 1)
        if parts:
            settings[parts[0]] = parts[1].strip() if len(parts) > 1 else ""
    expected = {
        "api_port": str(hub.port),
        "api_key_configured": "true",
        "api_jwt_secret_configured": "true",
        "aes_encryption_secret_configured": "true",
        "aws_delivery_queue": hub.delivery_queue,
        "aws_delivery_dlq": hub.delivery_queue + "-dlq",
        "aws_log_queue": hub.log_queue,
        "aws_log_dlq": hub.log_queue + "-dlq",
        "aws_region": "us-east-1",
        "deployment_id": hub.deployment,
        "log_batch_threshold_seconds": "1",
        "max_destinations_per_tenant": "7",
        "mq_type": "awssqs",
        "postgres_configured": "true",
        "postgres_host": f"{hub.engines.pg_host}:{hub.engines.pg_port}",
        "publish_max_redeliveries": "3",
        "redis_host": hub.engines.kv_host,
        "redis_port": str(hub.engines.kv_port),
        "retry_max_limit": "10",
        "retry_interval_seconds": "30",
        "retry_schedule": "[5 10]",
        "telemetry_disabled": "true",
        "topics": "[user.created user.updated user.deleted]",
        "alert_auto_disable_destination": "false",
        "alert_consecutive_failure_count": "100",
        "version": "224bff2",
    }
    assert {k: settings.get(k) for k in expected} == expected
    for secret in (hub.api_key, hub.jwt_secret, hub.env["AES_ENCRYPTION_SECRET"], "hub-pass-1"):
        assert secret not in result.stdout


def test_without_deployment_id(make_hub):
    hub = make_hub(start=False, DEPLOYMENT_ID=None)
    kv = hub.engines.valkey()
    tenant = "t-" + uid()
    try:
        plan = lines(hub.cli("migrate", "plan").stdout)
        assert "Redis Migrations (3 pending):" in plan, plan
        assert [line.split(":")[0] for line in plan if line.startswith("- redis/")] == \
            ["- redis/001_hash_tags", "- redis/002_timestamps", "- redis/003_entity"]
        hub.migrate()
        for name in ("001_hash_tags", "002_timestamps", "003_entity"):
            assert kv.hget(f"outpost:migration:{name}", "status") == b"applied"
        hub.start(migrate=False)
        assert hub.request("PUT", f"/tenants/{tenant}", {}).status == 201
        assert kv.hget(f"tenant:{{{tenant}}}:tenant", "id") == tenant.encode()
    finally:
        hub.stop()
        for pattern in ("outpost:migration:*", f"tenant:{{{tenant}}}:*", "rsmq:*", "outpostrc", ".outpost:*"):
            keys = list(kv.scan_iter(match=pattern))
            if keys:
                kv.delete(*keys)
