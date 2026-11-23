"""Multi-tenant workflows: two customers share the server but never each other's tokens, usage or caches."""

import time
from concurrent.futures import ThreadPoolExecutor

from checks import cached_token, check_row, check_token, failed_envelope, ok_envelope, relay_error, same, ttl_within
from conftest import SYNC

AFTER_TTL = SYNC + 1.5


def test_same_named_tokens_stay_with_their_owner(relay):
    a, b = relay.user("vip"), relay.user("svip")
    ta = relay.create_token(a, name="shared", remain_quota=111)
    tb = relay.create_token(b, name="shared", remain_quota=222, models="gpt-3.5-turbo")
    for user, own in ((a, ta), (b, tb)):
        status, reply = relay.api(user, "GET", "/api/token/?p=0")
        listed = ok_envelope(status, reply)
        assert [t["id"] for t in listed] == [own["id"]]
        check_token(listed[0], {**own})
        status, reply = relay.api(user, "GET", "/api/token/search?keyword=shared")
        assert [t["id"] for t in ok_envelope(status, reply)] == [own["id"]]

    snapshot = relay.token_row(tb["id"])
    attempts = [
        ("GET", f"/api/token/{tb['id']}", None),
        ("PUT", "/api/token/", {"id": tb["id"], "name": "stolen", "remain_quota": 1, "expired_time": -1, "unlimited_quota": True}),
        ("PUT", "/api/token/?status_only=1", {"id": tb["id"], "status": 2}),
        ("DELETE", f"/api/token/{tb['id']}", None),
    ]
    for method, path, body in attempts:
        status, reply = relay.api(a, method, path, body)
        failed_envelope(reply, status)
        assert relay.token_row(tb["id"]) == snapshot, (method, path)
    check_row(relay.token_row(ta["id"]), {"user_id": a["id"], "name": "shared", "remain_quota": 111, "status": 1})

    # B's own operations on the same id work and leave A's same-named token alone.
    status, reply = relay.api(b, "PUT", "/api/token/?status_only=1", {"id": tb["id"], "status": 2})
    check_token(ok_envelope(status, reply), {"id": tb["id"], "user_id": b["id"], "status": 2})
    status, reply = relay.api(b, "DELETE", f"/api/token/{tb['id']}")
    assert status == 200 and reply["success"] is True
    assert relay.token_row(tb["id"]) is None
    check_row(relay.token_row(ta["id"]), {"user_id": a["id"], "name": "shared", "remain_quota": 111, "status": 1})


def test_usage_is_charged_to_the_caller_only(relay, stubs, tag):
    a, b = relay.user("vip"), relay.user("svip")
    ta = relay.create_token(a, name="shared")
    ta_idle = relay.create_token(a, name="idle")
    tb = relay.create_token(b, name="shared")
    prompt = f"identical prompt {tag}"
    for key in (ta["key"], tb["key"], ta["key"]):
        status, reply = relay.chat(key, prompt)
        assert status == 200, reply
    assert [c["upstream"] for c in stubs.tagged(tag)] == ["vip", "svip", "vip"]
    relay.wait_for(lambda: relay.user_row(a["id"])["request_count"] == 2 and relay.user_row(b["id"])["request_count"] == 1)
    relay.wait_for(lambda: relay.token_row(ta["id"])["used_quota"] == 126)
    assert relay.user_row(a["id"]) == {"quota": 1_000_000 - 126, "used_quota": 126, "request_count": 2, "status": 1}
    assert relay.user_row(b["id"]) == {"quota": 1_000_000 - 63, "used_quota": 63, "request_count": 1, "status": 1}
    check_row(relay.token_row(ta["id"]), {"remain_quota": 500_000 - 126, "used_quota": 126})
    check_row(relay.token_row(ta_idle["id"]), {"remain_quota": 500_000, "used_quota": 0})
    check_row(relay.token_row(tb["id"]), {"remain_quota": 500_000 - 63, "used_quota": 63})

    logs_a, logs_b = relay.logs(a), relay.logs(b)
    assert [(e["user_id"], e["token_name"], e["quota"], e["channel"]) for e in logs_a] == [(a["id"], "shared", 63, relay.channel_ids["vip"])] * 2
    assert [(e["user_id"], e["token_name"], e["quota"], e["channel"]) for e in logs_b] == [(b["id"], "shared", 63, relay.channel_ids["svip"])]
    assert logs_a[0]["created_at"] >= logs_a[1]["created_at"]

    for user, quota, used, count in ((a, 1_000_000 - 126, 126, 2), (b, 1_000_000 - 63, 63, 1)):
        status, reply = relay.api(user, "GET", "/api/user/self")
        data = ok_envelope(status, reply)
        for field, value in (("id", user["id"]), ("quota", quota), ("used_quota", used), ("request_count", count)):
            same(data[field], value, field)


def test_cache_entries_are_keyed_per_user_and_key(relay, stubs, tag):
    a, b = relay.user("vip"), relay.user("svip")
    ta = relay.create_token(a, name="shared")
    tb = relay.create_token(b, name="shared")
    assert relay.kv.get(f"user_quota:{a['id']}") is None and relay.kv.get(f"token:{ta['key']}") is None
    status, _ = relay.chat(ta["key"], tag)
    assert status == 200
    status, _ = relay.chat(ta["key"], tag)
    assert status == 200
    # B has not relayed yet: none of A's entries were written under B's identity.
    for name in (f"token:{tb['key']}", f"user_quota:{b['id']}", f"user_group:{b['id']}", f"user_enabled:{b['id']}"):
        assert relay.kv.get(name) is None, name
    status, _ = relay.chat(tb["key"], tag)
    assert status == 200

    for user, token in ((a, ta), (b, tb)):
        cached = cached_token(relay, token["key"])
        for field, want in (("id", token["id"]), ("user_id", user["id"]), ("key", token["key"]), ("name", "shared"),
                            ("status", 1), ("unlimited_quota", False)):
            same(cached[field], want, f"token:{token['key']}.{field}")
        assert relay.kv.get(f"user_group:{user['id']}") == user["group"]
        assert relay.kv.get(f"user_enabled:{user['id']}") == "1"
        for name in (f"token:{token['key']}", f"user_quota:{user['id']}", f"user_group:{user['id']}", f"user_enabled:{user['id']}"):
            ttl_within(relay, name, SYNC)
    quota_a, quota_b = relay.kv.get(f"user_quota:{a['id']}"), relay.kv.get(f"user_quota:{b['id']}")
    assert quota_a.lstrip("-").isdigit() and quota_b.lstrip("-").isdigit(), (quota_a, quota_b)
    # Same starting balance, same prompt: two requests by A, one by B.
    assert int(quota_a) < int(quota_b) < 1_000_000, (quota_a, quota_b)

    time.sleep(AFTER_TTL)
    for name in (f"token:{ta['key']}", f"user_quota:{a['id']}", f"user_group:{a['id']}", f"user_enabled:{a['id']}",
                 f"token:{tb['key']}", f"user_quota:{b['id']}"):
        assert relay.kv.get(name) is None, f"{name} outlived SYNC_FREQUENCY"


def test_exhausted_customer_does_not_affect_another(relay, stubs, tag):
    a, b = relay.user("vip", quota=0), relay.user("svip", quota=5_000_000)
    ta = relay.create_token(a, name="shared")
    tb = relay.create_token(b, name="shared")
    for _ in range(2):
        status, reply = relay.chat(ta["key"], tag)
        relay_error(status, reply, 403, code="insufficient_user_quota")
        status, reply = relay.chat(tb["key"], tag)
        assert status == 200, reply
    assert [c["upstream"] for c in stubs.tagged(tag)] == ["svip", "svip"]
    relay.wait_for(lambda: relay.user_row(b["id"])["request_count"] == 2)
    assert relay.user_row(a["id"]) == {"quota": 0, "used_quota": 0, "request_count": 0, "status": 1}
    assert relay.user_row(b["id"]) == {"quota": 5_000_000 - 126, "used_quota": 126, "request_count": 2, "status": 1}
    check_row(relay.token_row(ta["id"]), {"remain_quota": 500_000, "used_quota": 0, "status": 1})
    assert relay.kv.get(f"user_quota:{a['id']}") == "0"
    assert int(relay.kv.get(f"user_quota:{b['id']}")) > 0
    assert relay.logs(a) == [] and len(relay.logs(b)) == 2


def test_model_excluded_for_one_customer_only(relay, stubs, tag):
    a, b = relay.user("vip"), relay.user("svip")
    ta = relay.create_token(a, name="shared", models="gpt-4")
    tb = relay.create_token(b, name="shared", models="gpt-4,gpt-3.5-turbo")
    status, reply = relay.chat(ta["key"], tag)
    relay_error(status, reply, 403)
    status, reply = relay.chat(tb["key"], tag)
    assert status == 200, reply
    assert [c["upstream"] for c in stubs.tagged(tag)] == ["svip"]
    relay.wait_for(lambda: relay.user_row(b["id"])["request_count"] == 1)
    assert relay.user_row(a["id"])["used_quota"] == 0 and relay.user_row(b["id"])["used_quota"] == 63


def test_concurrent_customers_keep_exact_totals(relay, stubs, tag):
    a, b = relay.user("vip"), relay.user("svip")
    ta = relay.create_token(a, name="same")
    tb = relay.create_token(b, name="same")
    jobs = [ta["key"]] * 12 + [tb["key"]] * 8
    with ThreadPoolExecutor(10) as pool:
        statuses = list(pool.map(lambda key: relay.chat(key, f"concurrent {tag}")[0], jobs))
    assert statuses == [200] * 20
    reached = sorted(c["upstream"] for c in stubs.tagged(tag))
    assert reached == ["svip"] * 8 + ["vip"] * 12
    relay.wait_for(lambda: relay.user_row(a["id"])["request_count"] == 12 and relay.user_row(b["id"])["request_count"] == 8)
    relay.wait_for(lambda: relay.token_row(ta["id"])["used_quota"] == 756 and relay.token_row(tb["id"])["used_quota"] == 504)
    assert relay.user_row(a["id"]) == {"quota": 1_000_000 - 756, "used_quota": 756, "request_count": 12, "status": 1}
    assert relay.user_row(b["id"]) == {"quota": 1_000_000 - 504, "used_quota": 504, "request_count": 8, "status": 1}
    check_row(relay.token_row(ta["id"]), {"remain_quota": 500_000 - 756, "used_quota": 756})
    check_row(relay.token_row(tb["id"]), {"remain_quota": 500_000 - 504, "used_quota": 504})
    assert len(relay.logs(a, 0)) == 10 and len(relay.logs(a, 1)) == 2 and len(relay.logs(b, 0)) == 8


def test_token_revocation_takes_effect_after_cache_expiry(relay, stubs, tag):
    a, b = relay.user("vip"), relay.user("svip")
    deleted = relay.create_token(a, name="deleted")
    disabled = relay.create_token(a, name="disabled")
    tb = relay.create_token(b, name="deleted")
    for key in (deleted["key"], disabled["key"], tb["key"]):
        assert relay.chat(key, f"warm {tag}")[0] == 200
    status, reply = relay.api(a, "DELETE", f"/api/token/{deleted['id']}")
    assert status == 200 and reply["success"] is True
    status, reply = relay.api(a, "PUT", "/api/token/?status_only=1", {"id": disabled["id"], "status": 2})
    check_token(ok_envelope(status, reply), {"status": 2})
    time.sleep(AFTER_TTL)
    status, reply = relay.chat(deleted["key"], f"late {tag}")
    relay_error(status, reply, 401)
    status, reply = relay.chat(disabled["key"], f"late {tag}")
    relay_error(status, reply, 401)
    status, reply = relay.chat(tb["key"], f"late {tag}")
    assert status == 200, reply
    assert [c["upstream"] for c in stubs.tagged(f"late {tag}")] == ["svip"]


def test_account_changes_take_effect_after_cache_expiry(relay, stubs, tag):
    a, b, c = relay.user("vip"), relay.user("svip"), relay.user("vip")
    ta, tb, tc = (relay.create_token(u, name="acct") for u in (a, b, c))
    for key in (ta["key"], tb["key"], tc["key"]):
        assert relay.chat(key, f"warm {tag}")[0] == 200
    relay.q("UPDATE users SET status = 2 WHERE id = %s", a["id"])
    relay.q('UPDATE users SET "group" = %s WHERE id = %s', "vip", b["id"])
    relay.q("UPDATE users SET quota = 0 WHERE id = %s", c["id"])
    time.sleep(AFTER_TTL)

    status, reply = relay.chat(ta["key"], f"late-a {tag}")
    relay_error(status, reply, 403)
    assert relay.kv.get(f"user_enabled:{a['id']}") == "0"
    status, reply = relay.chat(tb["key"], f"late-b {tag}")
    assert status == 200, reply
    assert [x["upstream"] for x in stubs.tagged(f"late-b {tag}")] == ["vip"]
    assert relay.kv.get(f"user_group:{b['id']}") == "vip"
    assert relay.kv.get(f"user_enabled:{b['id']}") == "1"
    status, reply = relay.chat(tc["key"], f"late-c {tag}")
    relay_error(status, reply, 403, code="insufficient_user_quota")
    assert stubs.tagged(f"late-a {tag}") == [] and stubs.tagged(f"late-c {tag}") == []
