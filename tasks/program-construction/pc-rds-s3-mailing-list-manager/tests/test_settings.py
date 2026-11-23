"""Settings: storage as rows, validation, secret handling, and domain rules applied to subscriber input."""

from pathlib import Path

from mailer import assert_import, assert_json, quiet_smtp

FIXTURES = Path(__file__).parent / "fixtures"
MASK = "•"


def setting(app, key):
    rows = app.q("SELECT value FROM settings WHERE key = %s", key)
    assert len(rows) == 1, (key, rows)
    return rows[0][0]


def test_settings_update_is_stored_per_key(bare_app):
    app = bare_app
    current = app.settings()
    assert current["upload.provider"] == "filesystem"
    assert_json(current["bounce.actions"], {"soft": {"count": 2, "action": "none"}, "hard": {"count": 1, "action": "blocklist"},
                                            "complaint": {"count": 1, "action": "blocklist"}})
    assert current["bounce.enabled"] is False and current["privacy.domain_blocklist"] == []
    assert [server["host"] for server in current["smtp"]] == ["smtp.yoursite.com", "smtp.gmail.com"]
    keys = {r[0] for r in app.q("SELECT key FROM settings")}

    def change(settings):
        quiet_smtp(settings)
        settings["app.site_name"] = "Renamed Site"
        settings["bounce.enabled"] = True
        settings["made.up.key"] = 1

    assert app.update_settings(change) == (200, {"data": True})
    assert setting(app, "app.site_name") == "Renamed Site"
    assert setting(app, "bounce.enabled") is True
    assert [(s["host"], s["port"], s["enabled"]) for s in setting(app, "smtp")] == [("127.0.0.1", 1, True), ("127.0.0.1", 1, False)]
    # Unknown keys are not stored.
    assert {r[0] for r in app.q("SELECT key FROM settings")} == keys
    after = app.settings()
    assert after["app.site_name"] == "Renamed Site" and after["bounce.enabled"] is True and "made.up.key" not in after
    assert [(s["host"], s["port"]) for s in after["smtp"]] == [("127.0.0.1", 1), ("127.0.0.1", 1)]


def test_settings_need_an_enabled_smtp_server(bare_app):
    app = bare_app
    before = app.q("SELECT key, value FROM settings ORDER BY key")

    def disable(settings):
        for server in settings["smtp"]:
            server["enabled"] = False
        settings["app.site_name"] = "Never Saved"

    status, payload = app.update_settings(disable)
    assert status == 400 and set(payload) == {"message"}
    assert app.q("SELECT key, value FROM settings ORDER BY key") == before
    assert app.settings()["app.site_name"] == "Mailing list"


def test_secrets_are_masked_and_kept(app, bucket):
    def s3(settings):
        settings.update({"upload.provider": "s3", "upload.s3.url": "http://engines:4566", "upload.s3.bucket": bucket,
                         "upload.s3.aws_access_key_id": "test", "upload.s3.aws_secret_access_key": "test-secret"})

    app.configure(s3)
    assert setting(app, "upload.s3.aws_secret_access_key") == "test-secret"
    shown = app.settings()
    assert shown["upload.s3.aws_secret_access_key"] == MASK * len("test-secret")
    assert shown["upload.s3.aws_access_key_id"] == "test"
    assert all(set(server["password"]) <= {MASK} for server in shown["smtp"])
    # An empty secret keeps the stored one.
    app.configure(lambda settings: settings.update({"upload.s3.aws_secret_access_key": "", "app.site_name": "Kept"}))
    assert setting(app, "upload.s3.aws_secret_access_key") == "test-secret"
    assert setting(app, "app.site_name") == "Kept"


def test_domain_blocklist(app):
    app.configure(lambda s: s.update({"privacy.domain_blocklist": ["Ban.NET ", "", "BaN.OrG", "*.Wild.test"]}))
    assert setting(app, "privacy.domain_blocklist") == ["ban.net", "ban.org", "*.wild.test"]
    assert app.settings()["privacy.domain_blocklist"] == ["ban.net", "ban.org", "*.wild.test"]
    expectations = {"x@ban.net": 400, "y@BAN.net": 400, "z@sub.ban.net": 200, "w@wild.test": 400, "v@a.wild.test": 400,
                    "u@a.b.wild.test": 200, "ok@fine.test": 200}
    for email, expected in expectations.items():
        status, _ = app.call("POST", "/api/subscribers", {"email": email})
        assert status == expected, (email, status)
    assert sorted(r[0] for r in app.q("SELECT email FROM subscribers WHERE email NOT LIKE '%%@example.com'")) == ["ok@fine.test", "u@a.b.wild.test", "z@sub.ban.net"]

    sid = app.q("SELECT id FROM subscribers WHERE email = 'ok@fine.test'")[0][0]
    for method, body in (("PATCH", {"email": "ok@ban.net"}), ("PUT", {"email": "ok@ban.org", "name": "x"})):
        status, _ = app.call(method, f"/api/subscribers/{sid}", body)
        assert status == 400, method
    assert app.q("SELECT email FROM subscribers WHERE id = %s", sid) == [("ok@fine.test",)]

    # Adapted from upstream frontend/cypress/e2e/subscribers.cy.js (domain blocklist import).
    list_id = app.create_list("Imported")["id"]
    final = app.run_import((FIXTURES / "subs-domain-blocklist.csv").read_text(), {"mode": "subscribe", "delim": ",", "lists": [list_id]})
    assert_import(final, {"name": "subscribers.csv", "total": 4, "imported": 2, "status": "finished"})
    assert sorted(r[0] for r in app.q("SELECT email FROM subscribers WHERE email LIKE '%%-import%%'")) == ["noban1-import@mail.com", "noban2-import1@mail.com"]

    # Clearing the list lets the domain in again.
    app.configure(lambda s: s.update({"privacy.domain_blocklist": []}))
    assert app.call("POST", "/api/subscribers", {"email": "x@ban.net"})[0] == 200


def test_domain_allowlist(app):
    app.configure(lambda s: s.update({"privacy.domain_allowlist": ["Allowed.test", "*.Corp.test"]}))
    assert setting(app, "privacy.domain_allowlist") == ["allowed.test", "*.corp.test"]
    for email, expected in {"a@allowed.test": 200, "b@other.test": 400, "c@corp.test": 200, "d@hq.corp.test": 200, "e@sub.allowed.test": 400}.items():
        status, _ = app.call("POST", "/api/subscribers", {"email": email})
        assert status == expected, (email, status)
    list_id = app.create_list("Allow")["id"]
    final = app.run_import("email,name\nin@allowed.test,In\nout@other.test,Out\n", {"mode": "subscribe", "delim": ",", "lists": [list_id]})
    assert_json([final["total"], final["imported"], final["status"]], [2, 1, "finished"])
    assert sorted(r[0] for r in app.q("SELECT email FROM subscribers WHERE email NOT LIKE '%%@example.com'")) == [
        "a@allowed.test", "c@corp.test", "d@hq.corp.test", "in@allowed.test"]
