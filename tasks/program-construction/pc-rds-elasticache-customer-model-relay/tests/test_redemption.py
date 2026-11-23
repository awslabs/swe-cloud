"""Redemption codes: administrator /api/redemption/ management, customer top-up and administrator top-up."""

import re
import secrets
import time

from checks import failed_envelope, number, ok_envelope

REDEMPTION_FIELDS = {"id", "user_id", "key", "status", "name", "quota", "created_time", "redeemed_time", "count"}


def _admin(relay):
    return relay.user("default", role=10)


def _codes(relay, admin, name=None, count=1, quota=700):
    name = name or "gift" + secrets.token_hex(2)
    status, reply = relay.api(admin, "POST", "/api/redemption/", {"name": name, "count": count, "quota": quota})
    keys = ok_envelope(status, reply)
    rows = relay.rows("SELECT * FROM redemptions WHERE key = ANY(%s) ORDER BY id", keys)
    assert len(rows) == len(keys), (keys, rows)
    return keys, rows


def _topup(relay, user, key):
    return relay.api(user, "POST", "/api/user/topup", {"key": key})


def _quota(relay, user):
    return relay.user_row(user["id"])["quota"]


def test_admin_creates_redemption_codes(relay):
    admin = _admin(relay)
    before = int(time.time()) - 1
    keys, rows = _codes(relay, admin, name="spring", count=3, quota=1234)
    assert len(keys) == 3 and len(set(keys)) == 3
    assert all(re.fullmatch(r"[0-9a-f]{32}", key) for key in keys), keys
    for row in rows:
        assert (row["user_id"], row["status"], row["name"], row["quota"], row["redeemed_time"]) == (admin["id"], 1, "spring", 1234, 0), row
        assert before <= row["created_time"] <= int(time.time()) + 1


def test_redemption_creation_validation(relay):
    admin = _admin(relay)
    tag = secrets.token_hex(3)
    for body in ({"name": "", "count": 1, "quota": 5}, {"name": "x" * 21, "count": 1, "quota": 5},
                 {"name": f"z{tag}", "count": 0, "quota": 5}, {"name": f"h{tag}", "count": 101, "quota": 5}):
        status, reply = relay.api(admin, "POST", "/api/redemption/", body)
        failed_envelope(reply, status)
    assert relay.rows("SELECT id FROM redemptions WHERE name IN (%s, %s, %s)", "x" * 21, f"z{tag}", f"h{tag}") == []
    keys, _ = _codes(relay, admin, name="y" * 20, count=100, quota=1)
    assert len(keys) == 100


def test_get_redemption_and_search_by_id(relay):
    admin = _admin(relay)
    _, [first, second] = _codes(relay, admin, count=2)
    status, reply = relay.api(admin, "GET", f"/api/redemption/{first['id']}")
    data = ok_envelope(status, reply)
    assert set(data) == REDEMPTION_FIELDS
    for field in REDEMPTION_FIELDS - {"count"}:
        assert data[field] == first[field], field
    number(data["count"], 0, "count")
    status, reply = relay.api(admin, "GET", f"/api/redemption/search?keyword={second['id']}")
    assert [r["id"] for r in ok_envelope(status, reply)] == [second["id"]]
    status, reply = relay.api(admin, "GET", "/api/redemption/999999")
    failed_envelope(reply, status)


def test_list_redemptions_newest_first(relay):
    admin = _admin(relay)
    _, rows = _codes(relay, admin, count=3)
    status, reply = relay.api(admin, "GET", "/api/redemption/?p=0")
    data = ok_envelope(status, reply)
    assert [r["id"] for r in data[:3]] == [row["id"] for row in reversed(rows)]
    assert all(set(r) == REDEMPTION_FIELDS for r in data)


def test_update_changes_name_and_quota_but_not_status(relay):
    admin = _admin(relay)
    _, [row] = _codes(relay, admin, quota=700)
    status, reply = relay.api(admin, "PUT", "/api/redemption/", {"id": row["id"], "name": "renamed", "quota": 900, "status": 2})
    data = ok_envelope(status, reply)
    assert (data["name"], data["quota"], data["status"]) == ("renamed", 900, 1), data
    stored = relay.rows("SELECT name, quota, status, key FROM redemptions WHERE id = %s", row["id"])[0]
    assert stored == {"name": "renamed", "quota": 900, "status": 1, "key": row["key"]}


def test_status_only_update_disables_a_code(relay):
    admin = _admin(relay)
    _, [row] = _codes(relay, admin, quota=700)
    status, reply = relay.api(admin, "PUT", "/api/redemption/?status_only=1", {"id": row["id"], "status": 2, "name": "ignored", "quota": 1})
    data = ok_envelope(status, reply)
    assert (data["name"], data["quota"], data["status"]) == (row["name"], 700, 2), data
    customer = relay.user("vip", quota=10)
    status, reply = _topup(relay, customer, row["key"])
    failed_envelope(reply, status)
    assert _quota(relay, customer) == 10
    assert relay.rows("SELECT status, redeemed_time FROM redemptions WHERE id = %s", row["id"])[0] == {"status": 2, "redeemed_time": 0}


def test_delete_redemption(relay):
    admin = _admin(relay)
    _, [gone, kept] = _codes(relay, admin, count=2)
    status, reply = relay.api(admin, "DELETE", f"/api/redemption/{gone['id']}")
    ok_envelope(status, reply)
    assert relay.rows("SELECT id FROM redemptions WHERE id = %s", gone["id"]) == []
    assert relay.rows("SELECT id FROM redemptions WHERE id = %s", kept["id"]) != []
    customer = relay.user("vip", quota=10)
    status, reply = _topup(relay, customer, gone["key"])
    failed_envelope(reply, status)
    assert _quota(relay, customer) == 10


def test_topup_adds_quota_and_marks_code_used(relay):
    admin = _admin(relay)
    _, [row] = _codes(relay, admin, quota=5000)
    customer = relay.user("vip", quota=1000)
    before = int(time.time()) - 1
    status, reply = _topup(relay, customer, row["key"])
    number(ok_envelope(status, reply), 5000, "top-up amount")
    assert _quota(relay, customer) == 6000
    stored = relay.rows("SELECT status, redeemed_time FROM redemptions WHERE id = %s", row["id"])[0]
    assert stored["status"] == 3 and before <= stored["redeemed_time"] <= int(time.time()) + 1, stored


def test_used_unknown_and_missing_codes_are_refused(relay):
    admin = _admin(relay)
    _, [row] = _codes(relay, admin, quota=5000)
    first, second = relay.user("vip", quota=10), relay.user("svip", quota=20)
    ok_envelope(*_topup(relay, first, row["key"]))
    for user, body in ((first, {"key": row["key"]}), (second, {"key": row["key"]}), (second, {"key": "0" * 32}), (second, {})):
        status, reply = relay.api(user, "POST", "/api/user/topup", body)
        failed_envelope(reply, status)
    assert (_quota(relay, first), _quota(relay, second)) == (5010, 20)


def test_topup_writes_a_topup_log_entry(relay):
    admin = _admin(relay)
    _, [row] = _codes(relay, admin, quota=5000)
    customer = relay.user("vip", quota=0)
    ok_envelope(*_topup(relay, customer, row["key"]))
    [entry] = relay.logs(customer)
    for field, value in (("user_id", customer["id"]), ("type", 1), ("username", customer["username"]), ("token_name", ""),
                         ("model_name", ""), ("quota", 0), ("prompt_tokens", 0), ("completion_tokens", 0), ("channel", 0)):
        assert entry[field] == value, (field, entry[field])
    assert isinstance(entry["content"], str) and entry["content"]


def test_admin_topup_endpoint(relay):
    admin = _admin(relay)
    customer = relay.user("vip", quota=100)
    status, reply = relay.api(admin, "POST", "/api/topup", {"user_id": customer["id"], "quota": 2500, "remark": "goodwill credit"})
    ok_envelope(status, reply)
    assert _quota(relay, customer) == 2600
    [entry] = relay.logs(customer)
    assert (entry["type"], entry["content"], entry["user_id"]) == (1, "goodwill credit", customer["id"]), entry
    status, reply = relay.api(customer, "POST", "/api/topup", {"user_id": customer["id"], "quota": 2500})
    failed_envelope(reply, status)
    assert _quota(relay, customer) == 2600


def test_customers_cannot_manage_redemptions(relay):
    admin = _admin(relay)
    _, [row] = _codes(relay, admin)
    customer = relay.user("vip")
    for method, path, body in (("GET", "/api/redemption/?p=0", None), ("GET", f"/api/redemption/{row['id']}", None),
                               ("POST", "/api/redemption/", {"name": "mine", "count": 1, "quota": 99999}),
                               ("DELETE", f"/api/redemption/{row['id']}", None)):
        status, reply = relay.api(customer, method, path, body)
        failed_envelope(reply, status)
    assert relay.rows("SELECT id FROM redemptions WHERE id = %s", row["id"]) != []
    assert relay.rows("SELECT id FROM redemptions WHERE user_id = %s", customer["id"]) == []
