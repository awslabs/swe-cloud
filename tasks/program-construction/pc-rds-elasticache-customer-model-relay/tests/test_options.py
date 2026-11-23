"""Root-only server options: /api/option/ listing and updates, and their immediate effect on status, pricing and registration.

These run on the second server (its own database), so option changes never reach the main server's tests.
"""

import json

from checks import failed_envelope, ok_envelope
from conftest import GROUP_RATIO


def _root(server):
    return server.user("default", role=100)


def _options(server, root):
    status, reply = server.api(root, "GET", "/api/option/")
    data = ok_envelope(status, reply)
    assert all(set(item) == {"key", "value"} and isinstance(item["value"], str) for item in data), data[:3]
    return {item["key"]: item["value"] for item in data}


def _put(server, user, key, value):
    return server.api(user, "PUT", "/api/option/", {"key": key, "value": value})


def _stored(server, key):
    found = server.rows("SELECT value FROM options WHERE key = %s", key)
    return found[0]["value"] if found else None


def test_options_need_root(limited):
    admin, customer = limited.user("default", role=10), limited.user("vip")
    for user in (admin, customer):
        status, reply = limited.api(user, "GET", "/api/option/")
        failed_envelope(reply, status)
        status, reply = _put(limited, user, "Notice", "hijacked")
        failed_envelope(reply, status)
    assert _stored(limited, "Notice") in (None, "")


def test_option_listing_includes_pricing_tables(limited):
    options = _options(limited, _root(limited))
    assert json.loads(options["GroupRatio"]) == GROUP_RATIO
    model_ratio = json.loads(options["ModelRatio"])
    assert model_ratio["gpt-3.5-turbo"] == 0.25 and model_ratio["gpt-4"] == 15, {k: model_ratio[k] for k in ("gpt-3.5-turbo", "gpt-4")}


def test_option_update_is_stored_and_listed(limited):
    root = _root(limited)
    status, reply = _put(limited, root, "Notice", "maintenance at noon")
    ok_envelope(status, reply)
    assert _stored(limited, "Notice") == "maintenance at noon"
    assert _options(limited, root)["Notice"] == "maintenance at noon"
    status, reply = limited.call("GET", "/api/notice")
    assert ok_envelope(status, reply) == "maintenance at noon"


def test_secret_options_are_never_listed(limited):
    root = _root(limited)
    for key in ("SMTPToken", "GitHubClientSecret"):
        ok_envelope(*_put(limited, root, key, f"value-of-{key}"))
        assert _stored(limited, key) == f"value-of-{key}"
    options = _options(limited, root)
    assert "SMTPToken" not in options and "GitHubClientSecret" not in options
    assert not [key for key in options if key.endswith(("Token", "Secret"))], sorted(options)


def test_system_name_option_appears_in_status(limited):
    root = _root(limited)
    ok_envelope(*_put(limited, root, "SystemName", "Relay Test"))
    status, reply = limited.call("GET", "/api/status")
    assert ok_envelope(status, reply)["system_name"] == "Relay Test"


def test_option_values_must_be_strings(limited):
    root = _root(limited)
    for value in (5, True):
        status, reply = _put(limited, root, "QuotaRemindThreshold", value)
        failed_envelope(reply, status, http_status=400)
    assert _stored(limited, "QuotaRemindThreshold") != "5"


def test_group_ratio_update_changes_price_immediately(limited, stubs, tag):
    root = _root(limited)
    user = limited.user("vip")
    token = limited.create_token(user, name="priced")
    try:
        ok_envelope(*_put(limited, root, "GroupRatio", json.dumps({**GROUP_RATIO, "vip": 4})))
        assert json.loads(_stored(limited, "GroupRatio"))["vip"] == 4
        status, reply = limited.chat(token["key"], tag)
        assert status == 200, reply
        limited.wait_for(lambda: limited.user_row(user["id"])["used_quota"] == 250)
        assert limited.user_row(user["id"])["used_quota"] == 250
    finally:
        _put(limited, root, "GroupRatio", json.dumps(GROUP_RATIO))


def test_quota_for_new_user_applies_to_registration(limited):
    root = _root(limited)
    try:
        ok_envelope(*_put(limited, root, "QuotaForNewUser", "1234"))
        name = "w" + root["username"][1:9]
        status, reply = limited.call("POST", "/api/user/register", {"username": name, "password": "secret-pass1"})
        ok_envelope(status, reply)
        assert limited.rows("SELECT quota FROM users WHERE username = %s", name)[0]["quota"] == 1234
    finally:
        _put(limited, root, "QuotaForNewUser", "0")


def test_registration_can_be_switched_off(limited):
    root = _root(limited)
    try:
        ok_envelope(*_put(limited, root, "RegisterEnabled", "false"))
        name = "x" + root["username"][1:9]
        status, reply = limited.call("POST", "/api/user/register", {"username": name, "password": "secret-pass1"})
        failed_envelope(reply, status)
        assert limited.rows("SELECT id FROM users WHERE username = %s", name) == []
    finally:
        _put(limited, root, "RegisterEnabled", "true")
