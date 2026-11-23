"""Multi-step interactions: import + segmentation + bounces, and state surviving reloads and restarts."""

import time

from mailer import ENDPOINT, App, assert_json, quiet_smtp, start_app


def membership_map(app, pattern):
    rows = app.q("SELECT s.email, s.status::text, l.name, sl.status::text FROM subscribers s JOIN subscriber_lists sl ON sl.subscriber_id = s.id "
                 "JOIN lists l ON l.id = sl.list_id WHERE s.email LIKE %s ORDER BY 1, 3", pattern)
    return [tuple(r) for r in rows]


def wait_rows(app, sql, expected, timeout=10):
    deadline = time.time() + timeout
    while True:
        rows = app.q(sql)
        if rows == expected or time.time() > deadline:
            return rows
        time.sleep(0.2)


def test_import_segment_and_bounce(app):
    newsletter = app.create_list("Newsletter", "public", "double")["id"]
    vip = app.create_list("VIP", "private", "single")["id"]
    csv = ("email,name,attributes\n"
           'amy@flow.test,Amy,"{""plan"": ""pro"", ""spend"": 120}"\n'
           'bob@flow.test,Bob,"{""plan"": ""free"", ""spend"": 0}"\n'
           'cyd@flow.test,Cyd,"{""plan"": ""pro"", ""spend"": 40}"\n'
           'dee@flow.test,Dee,"{""plan"": ""pro"", ""spend"": 300}"\n')
    final = app.run_import(csv, {"mode": "subscribe", "delim": ",", "lists": [newsletter], "subscription_status": "confirmed"})
    assert_json([final["imported"], final["status"]], [4, "finished"])

    segment = "subscribers.attribs->>'plan' = 'pro' AND (subscribers.attribs->>'spend')::INT >= 100"
    assert sorted(s["email"] for s in app.query_subscribers(query=segment)["results"]) == ["amy@flow.test", "dee@flow.test"]
    app.ok("PUT", "/api/subscribers/query/lists", {"query": segment, "action": "add", "target_list_ids": [vip], "status": "confirmed"})

    app.configure(lambda s: s.update({"bounce.enabled": True, "bounce.webhooks_enabled": True,
                                      "bounce.actions": {"soft": {"count": 1, "action": "unsubscribe"}, "hard": {"count": 2, "action": "blocklist"},
                                                         "complaint": {"count": 1, "action": "delete"}}}))
    app.ok("POST", "/webhooks/bounce", {"email": "dee@flow.test", "source": "mta", "type": "soft"})
    app.ok("POST", "/webhooks/bounce", {"email": "bob@flow.test", "source": "mta", "type": "hard"})
    app.ok("POST", "/webhooks/bounce", {"email": "cyd@flow.test", "source": "fbl", "type": "complaint"})
    app.ok("POST", "/webhooks/bounce", {"email": "bob@flow.test", "source": "mta", "type": "hard"})
    counted = ("SELECT s.email, b.type::text FROM bounces b JOIN subscribers s ON s.id = b.subscriber_id ORDER BY b.id")
    assert wait_rows(app, counted, [("dee@flow.test", "soft"), ("bob@flow.test", "hard"), ("bob@flow.test", "hard")]) == [
        ("dee@flow.test", "soft"), ("bob@flow.test", "hard"), ("bob@flow.test", "hard")]
    assert wait_rows(app, "SELECT email FROM subscribers WHERE email = 'cyd@flow.test'", []) == []
    time.sleep(0.5)
    assert membership_map(app, "%@flow.test") == [
        ("amy@flow.test", "enabled", "Newsletter", "confirmed"),
        ("amy@flow.test", "enabled", "VIP", "confirmed"),
        ("bob@flow.test", "blocklisted", "Newsletter", "confirmed"),
        ("dee@flow.test", "enabled", "Newsletter", "unsubscribed"),
        ("dee@flow.test", "enabled", "VIP", "unsubscribed"),
    ]
    confirmed = app.query_subscribers(list_id=newsletter, subscription_status="confirmed")
    assert sorted(s["email"] for s in confirmed["results"]) == ["amy@flow.test", "bob@flow.test"]
    # Explicit blocklisting of bounced subscribers then unsubscribes them too.
    app.ok("PUT", "/api/bounces/blocklist")
    assert membership_map(app, "%@flow.test") == [
        ("amy@flow.test", "enabled", "Newsletter", "confirmed"),
        ("amy@flow.test", "enabled", "VIP", "confirmed"),
        ("bob@flow.test", "blocklisted", "Newsletter", "unsubscribed"),
        ("dee@flow.test", "blocklisted", "Newsletter", "unsubscribed"),
        ("dee@flow.test", "blocklisted", "VIP", "unsubscribed"),
    ]


def test_state_survives_reload_and_restart(database, bucket):
    app = start_app(database)
    try:
        list_id = app.create_list("Persistent", "public", "single")["id"]
        sub = app.create_subscriber("keep@flow.test", attribs={"n": 1}, lists=[list_id], preconfirm_subscriptions=True)

        def change(settings):
            quiet_smtp(settings)
            settings.update({"upload.provider": "s3", "upload.s3.url": ENDPOINT, "upload.s3.bucket": bucket, "upload.s3.bucket_path": "/kept",
                             "upload.s3.aws_access_key_id": "test", "upload.s3.aws_secret_access_key": "test",
                             "privacy.domain_blocklist": ["blocked.test"], "bounce.enabled": True, "bounce.webhooks_enabled": True})

        app.configure(change)
        # Data written before the reload is intact and the reloaded settings are in effect.
        assert app.get(f"/api/subscribers/{sub['id']}")["lists"][0]["subscription_status"] == "confirmed"
        assert app.call("POST", "/api/subscribers", {"email": "x@blocked.test"})[0] == 400
        assert app.upload("one.txt", "text/plain", b"1")[0] == 200
    finally:
        app.stop()

    # A fresh process on the same database serves the same data, settings and credentials.
    restarted = App(database, app.token)
    try:
        assert restarted.get(f"/api/lists/{list_id}")["name"] == "Persistent"
        assert restarted.call("POST", "/api/subscribers", {"email": "y@blocked.test"})[0] == 400
        status, payload = restarted.upload("two.txt", "text/plain", b"2")
        assert status == 200 and payload["data"]["url"] == f"{ENDPOINT}/{bucket}/kept/two.txt"
        assert sorted(item["filename"] for item in restarted.get("/api/media")["results"]) == ["one.txt", "two.txt"]
        assert restarted.ok("POST", "/webhooks/bounce", {"email": "keep@flow.test", "source": "x", "type": "hard"}) is True
        assert wait_rows(restarted, "SELECT status::text FROM subscribers WHERE email = 'keep@flow.test'", [("blocklisted",)]) == [("blocklisted",)]
    finally:
        restarted.stop()
