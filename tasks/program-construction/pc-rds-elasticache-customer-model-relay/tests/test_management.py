"""Customer token management API: one operation at a time, checked over HTTP and in Postgres."""

import time

from checks import TOKEN_FIELDS, check_row, check_token, failed_envelope, number, ok_envelope


def _create(relay, user, body):
    return relay.api(user, "POST", "/api/token/", body)


def test_create_token_returns_and_stores_record(relay):
    a = relay.user()
    before = int(time.time()) - 1
    status, reply = _create(relay, a, {"name": "build-bot", "remain_quota": 1234, "unlimited_quota": False,
                                       "expired_time": 4102444800, "models": "gpt-3.5-turbo", "subnet": "10.0.0.0/8,127.0.0.1/32"})
    token = ok_envelope(status, reply)
    after = int(time.time()) + 1
    check_token(token, {"user_id": a["id"], "status": 1, "name": "build-bot", "expired_time": 4102444800, "remain_quota": 1234,
                        "unlimited_quota": False, "used_quota": 0, "models": "gpt-3.5-turbo", "subnet": "10.0.0.0/8,127.0.0.1/32"},
                created_window=(before, after))
    key = token["key"]
    assert isinstance(key, str) and len(key) == 48 and key.isalnum() and key.isascii() and not key.startswith("sk-"), key
    row = relay.token_row(token["id"])
    check_row(row, {"user_id": a["id"], "key": key, "status": 1, "name": "build-bot", "expired_time": 4102444800, "remain_quota": 1234,
                    "unlimited_quota": False, "used_quota": 0, "models": "gpt-3.5-turbo", "subnet": "10.0.0.0/8,127.0.0.1/32",
                    "created_time": token["created_time"]})

    status, reply = _create(relay, a, {"name": "minimal"})
    second = ok_envelope(status, reply)
    check_token(second, {"user_id": a["id"], "status": 1, "name": "minimal", "remain_quota": 0, "unlimited_quota": False,
                         "used_quota": 0, "models": None, "subnet": None})
    assert second["key"] != key and second["id"] != token["id"]
    assert [t["id"] for t in relay.tokens_of(a["id"])] == sorted([token["id"], second["id"]])


def test_create_ignores_client_controlled_fields(relay):
    a, b = relay.user(), relay.user()
    forged = {"id": 987654, "user_id": b["id"], "key": "k" * 48, "status": 2, "used_quota": 77, "name": "forged",
              "remain_quota": 10, "expired_time": -1, "unlimited_quota": True}
    token = ok_envelope(*_create(relay, a, forged))
    check_token(token, {"user_id": a["id"], "status": 1, "used_quota": 0, "name": "forged", "remain_quota": 10, "unlimited_quota": True,
                        "expired_time": -1})
    assert token["key"] != "k" * 48 and token["id"] != 987654
    check_row(relay.token_row(token["id"]), {"user_id": a["id"], "status": 1, "used_quota": 0, "key": token["key"]})
    assert relay.tokens_of(b["id"]) == []


def test_create_validation_rejects_without_inserting(relay):
    a = relay.user()
    refused = [
        {"name": "x" * 31},                      # names are limited to 30 bytes
        {"name": "令" * 11},                     # 33 bytes of UTF-8
        {"name": "net", "subnet": "10.0.0.0/33"},
        {"name": "net", "subnet": "not-a-network"},
        {"name": "q", "remain_quota": "10"},     # wrong JSON type
    ]
    for body in refused:
        status, reply = _create(relay, a, {"expired_time": -1, **body})
        failed_envelope(reply, status)
    for raw in (b"{nope", b""):
        status, reply = relay.api(a, "POST", "/api/token/", raw=raw)
        failed_envelope(reply, status)
    assert relay.tokens_of(a["id"]) == []

    accepted = [("y" * 30), ("令" * 10)]
    for name in accepted:
        token = ok_envelope(*_create(relay, a, {"name": name, "expired_time": -1}))
        assert token["name"] == name
    assert sorted(t["name"] for t in relay.tokens_of(a["id"])) == sorted(accepted)


def test_list_pages_newest_first(relay):
    a, b = relay.user(), relay.user()
    ids = [relay.create_token(a, name=f"t{i}", remain_quota=i)["id"] for i in range(13)]
    relay.create_token(b, name="t0")
    newest = sorted(ids, reverse=True)
    for query, expected in (("?p=0", newest[:10]), ("?p=1", newest[10:]), ("?p=2", []), ("", newest[:10]),
                            ("?p=-3", newest[:10]), ("?p=abc", newest[:10])):
        status, reply = relay.api(a, "GET", "/api/token/" + query)
        data = ok_envelope(status, reply)
        assert [t["id"] for t in data] == expected, query
        for t in data:
            check_token(t, {"user_id": a["id"], "name": f"t{ids.index(t['id'])}", "remain_quota": ids.index(t["id"])})
            assert relay.token_row(t["id"])["key"] == t["key"]


def test_list_order_modes(relay):
    a = relay.user()
    made = {}
    for name, remain, used, unlimited in (("small", 10, 500, False), ("big", 9000, 20, False), ("mid", 300, 100, False),
                                          ("free", 1, 0, True), ("zero", 0, 900, False)):
        made[name] = relay.create_token(a, name=name, remain_quota=remain, unlimited_quota=unlimited)["id"]
        relay.q("UPDATE tokens SET used_quota = %s WHERE id = %s", used, made[name])

    def names(order):
        status, reply = relay.api(a, "GET", f"/api/token/?p=0&order={order}")
        return [t["name"] for t in ok_envelope(status, reply)]

    assert names("remain_quota") == ["free", "big", "mid", "small", "zero"]
    assert names("used_quota") == ["zero", "small", "mid", "big", "free"]
    assert names("anything-else") == ["zero", "free", "mid", "big", "small"]


def test_search_matches_name_prefix_of_own_tokens(relay):
    a, b = relay.user(), relay.user()
    for name in ("alpha-1", "alpha-2", "alpha-2", "beta", "xalpha"):
        relay.create_token(a, name=name)
    relay.create_token(b, name="alpha-3")

    def search(user, keyword):
        status, reply = relay.api(user, "GET", f"/api/token/search?keyword={keyword}")
        data = ok_envelope(status, reply)
        for t in data:
            assert set(t) == TOKEN_FIELDS and t["user_id"] == user["id"]
        return sorted(t["name"] for t in data)

    assert search(a, "alpha") == ["alpha-1", "alpha-2", "alpha-2"]
    assert search(a, "alpha-2") == ["alpha-2", "alpha-2"]
    assert search(a, "") == ["alpha-1", "alpha-2", "alpha-2", "beta", "xalpha"]
    assert search(a, "gamma") == []
    assert search(b, "alpha") == ["alpha-3"]


def test_get_single_token(relay):
    a = relay.user()
    created = relay.create_token(a, name="one", remain_quota=42, models="gpt-3.5-turbo")
    status, reply = relay.api(a, "GET", f"/api/token/{created['id']}")
    check_token(ok_envelope(status, reply), {**created})
    for path in ("/api/token/999999999", "/api/token/abc", "/api/token/0"):
        status, reply = relay.api(a, "GET", path)
        failed_envelope(reply, status)


def test_full_update_replaces_editable_fields(relay):
    a, b = relay.user(), relay.user()
    created = relay.create_token(a, name="orig", remain_quota=50, models="gpt-3.5-turbo", subnet="10.0.0.0/8")
    status, reply = relay.api(a, "PUT", "/api/token/", {
        "id": created["id"], "name": "renamed", "remain_quota": 77, "expired_time": 4102444800, "unlimited_quota": True,
        "models": "gpt-4", "subnet": "", "status": 2, "used_quota": 5, "user_id": b["id"], "key": "z" * 48})
    data = ok_envelope(status, reply)
    expected = {"id": created["id"], "user_id": a["id"], "key": created["key"], "status": 1, "name": "renamed", "remain_quota": 77,
                "expired_time": 4102444800, "unlimited_quota": True, "models": "gpt-4", "subnet": "", "used_quota": 0}
    check_token(data, expected)
    check_row(relay.token_row(created["id"]), {k: v for k, v in expected.items() if k != "id"})

    # Every editable field is replaced, so omitted fields take their zero values.
    status, reply = relay.api(a, "PUT", "/api/token/", {"id": created["id"]})
    data = ok_envelope(status, reply)
    blank = {"user_id": a["id"], "key": created["key"], "status": 1, "name": "", "remain_quota": 0, "expired_time": 0,
             "unlimited_quota": False, "models": None, "subnet": None, "used_quota": 0}
    check_token(data, blank)
    check_row(relay.token_row(created["id"]), blank)


def test_status_only_update_changes_only_status(relay):
    a = relay.user()
    created = relay.create_token(a, name="keep", remain_quota=900, models="gpt-3.5-turbo", expired_time=4102444800)
    status, reply = relay.api(a, "PUT", "/api/token/?status_only=1", {"id": created["id"], "status": 2, "name": "ignored",
                                                                       "remain_quota": 1, "models": "", "expired_time": -1})
    data = ok_envelope(status, reply)
    kept = {"user_id": a["id"], "key": created["key"], "name": "keep", "remain_quota": 900, "models": "gpt-3.5-turbo",
            "expired_time": 4102444800, "unlimited_quota": False, "used_quota": 0}
    check_token(data, {**kept, "status": 2})
    check_row(relay.token_row(created["id"]), {**kept, "status": 2})
    status, reply = relay.api(a, "PUT", "/api/token/?status_only=true", {"id": created["id"], "status": 1})
    check_token(ok_envelope(status, reply), {**kept, "status": 1})
    check_row(relay.token_row(created["id"]), {**kept, "status": 1})


def test_update_refusals_leave_row_unchanged(relay):
    a = relay.user()
    created = relay.create_token(a, name="stable", remain_quota=5)
    snapshot = relay.token_row(created["id"])
    for path, body in (("/api/token/", {"id": created["id"], "name": "n" * 31, "expired_time": -1}),
                       ("/api/token/", {"id": created["id"], "name": "ok", "subnet": "300.1.1.1/8"}),
                       ("/api/token/", {"name": "no id"}),
                       ("/api/token/", {"id": 999999999, "name": "missing"}),
                       ("/api/token/?status_only=1", {"id": 999999999, "status": 2})):
        status, reply = relay.api(a, "PUT", path, body)
        failed_envelope(reply, status)
        assert relay.token_row(created["id"]) == snapshot


def test_reenable_guards_for_expired_and_exhausted_tokens(relay):
    a = relay.user()
    expired = relay.create_token(a, name="old", expired_time=1000, remain_quota=10)
    relay.q("UPDATE tokens SET status = 3 WHERE id = %s", expired["id"])
    snapshot = relay.token_row(expired["id"])
    for path, body in (("/api/token/?status_only=1", {"id": expired["id"], "status": 1}),
                       ("/api/token/", {"id": expired["id"], "status": 1, "name": "old", "expired_time": 1000, "remain_quota": 10})):
        status, reply = relay.api(a, "PUT", path, body)
        failed_envelope(reply, status)
        assert relay.token_row(expired["id"]) == snapshot
    # A full update without status keeps the expired status but may move the expiry.
    status, reply = relay.api(a, "PUT", "/api/token/", {"id": expired["id"], "name": "old", "expired_time": -1, "remain_quota": 10})
    check_token(ok_envelope(status, reply), {"status": 3, "expired_time": -1, "name": "old", "remain_quota": 10})
    check_row(relay.token_row(expired["id"]), {"status": 3, "expired_time": -1})
    status, reply = relay.api(a, "PUT", "/api/token/?status_only=1", {"id": expired["id"], "status": 1})
    check_token(ok_envelope(status, reply), {"status": 1, "expired_time": -1})
    check_row(relay.token_row(expired["id"]), {"status": 1})

    exhausted = relay.create_token(a, name="empty", remain_quota=0)
    relay.q("UPDATE tokens SET status = 4 WHERE id = %s", exhausted["id"])
    snapshot = relay.token_row(exhausted["id"])
    for path, body in (("/api/token/?status_only=1", {"id": exhausted["id"], "status": 1}),
                       ("/api/token/", {"id": exhausted["id"], "status": 1, "name": "empty", "remain_quota": 50, "expired_time": -1})):
        status, reply = relay.api(a, "PUT", path, body)
        failed_envelope(reply, status)
        assert relay.token_row(exhausted["id"]) == snapshot
    # Moving it to "disabled" is not an enable and is allowed.
    status, reply = relay.api(a, "PUT", "/api/token/?status_only=1", {"id": exhausted["id"], "status": 2})
    check_token(ok_envelope(status, reply), {"status": 2, "remain_quota": 0})
    check_row(relay.token_row(exhausted["id"]), {"status": 2})

    # Status 3 whose expiry lies in the future can be re-enabled.
    revived = relay.create_token(a, name="future", expired_time=4102444800)
    relay.q("UPDATE tokens SET status = 3 WHERE id = %s", revived["id"])
    status, reply = relay.api(a, "PUT", "/api/token/?status_only=1", {"id": revived["id"], "status": 1})
    check_token(ok_envelope(status, reply), {"status": 1, "expired_time": 4102444800})
    check_row(relay.token_row(revived["id"]), {"status": 1})


def test_delete_token(relay):
    a = relay.user()
    keep = relay.create_token(a, name="keep")
    gone = relay.create_token(a, name="gone")
    status, reply = relay.api(a, "DELETE", f"/api/token/{gone['id']}")
    assert status == 200 and reply["success"] is True, reply
    assert relay.token_row(gone["id"]) is None
    assert relay.token_row(keep["id"]) is not None
    status, reply = relay.api(a, "GET", "/api/token/?p=0")
    assert [t["id"] for t in ok_envelope(status, reply)] == [keep["id"]]
    for method, path in (("GET", f"/api/token/{gone['id']}"), ("DELETE", f"/api/token/{gone['id']}"), ("DELETE", "/api/token/abc")):
        status, reply = relay.api(a, method, path)
        failed_envelope(reply, status)
    assert [t["id"] for t in relay.tokens_of(a["id"])] == [keep["id"]]


def test_management_authentication(relay):
    a = relay.user()
    relay.create_token(a, name="mine")
    status, reply = relay.call("GET", "/api/token/?p=0")
    failed_envelope(reply, status, http_status=401)
    status, reply = relay.call("GET", "/api/token/?p=0", auth="0" * 32)
    failed_envelope(reply, status)
    status, reply = relay.call("POST", "/api/token/", {"name": "x", "expired_time": -1}, auth="f" * 32)
    failed_envelope(reply, status)

    disabled = relay.user(status=2)
    for method, path, body in (("GET", "/api/token/?p=0", None), ("POST", "/api/token/", {"name": "x", "expired_time": -1}),
                               ("GET", "/api/user/self", None)):
        status, reply = relay.api(disabled, method, path, body)
        failed_envelope(reply, status)
    assert relay.tokens_of(disabled["id"]) == []
    assert [t["name"] for t in relay.tokens_of(a["id"])] == ["mine"]


def test_user_self_reports_own_account(relay):
    a = relay.user(group="svip", quota=4321)
    relay.q("UPDATE users SET used_quota = 17, request_count = 3 WHERE id = %s", a["id"])
    b = relay.user(group="vip", quota=99)
    status, reply = relay.api(a, "GET", "/api/user/self")
    data = ok_envelope(status, reply)
    for field, value in (("username", a["username"]), ("display_name", a["username"]), ("group", "svip"), ("access_token", a["access"])):
        assert data[field] == value and isinstance(data[field], str), (field, data[field])
    for field, value in (("id", a["id"]), ("role", 1), ("status", 1)):
        number(data[field], value, field)
    number(data["quota"], 4321, "quota")
    number(data["used_quota"], 17, "used_quota")
    number(data["request_count"], 3, "request_count")
    assert data.get("password") in (None, ""), "the password must not be disclosed"
    status, reply = relay.api(b, "GET", "/api/user/self")
    data = ok_envelope(status, reply)
    number(data["id"], b["id"], "id")
    assert data["group"] == "vip"
    number(data["quota"], 99, "quota")
