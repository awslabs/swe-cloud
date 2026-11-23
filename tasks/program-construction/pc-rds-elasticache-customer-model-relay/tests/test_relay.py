"""Single relay requests: forwarding, pricing, and every refusal branch with its preserved state."""

import time

import pytest

from checks import number, relay_error, same
from conftest import SYNC

MODEL = "gpt-3.5-turbo"


def _accounts(relay, user, token):
    return relay.user_row(user["id"]), relay.rows("SELECT remain_quota, used_quota, status FROM tokens WHERE id = %s", token["id"])[0]


def _charged(relay, user, token, before_user, before_token, amount):
    """Wait for the asynchronous counters, then require exactly `amount` on both user and token."""
    relay.wait_for(lambda: relay.user_row(user["id"])["used_quota"] != before_user["used_quota"])
    relay.wait_for(lambda: relay.token_row(token["id"])["used_quota"] != before_token["used_quota"])
    after_user, after_token = _accounts(relay, user, token)
    assert after_user["quota"] == before_user["quota"] - amount, (before_user, after_user)
    assert after_user["used_quota"] == before_user["used_quota"] + amount, (before_user, after_user)
    assert after_user["request_count"] == before_user["request_count"] + 1, (before_user, after_user)
    assert after_token["remain_quota"] == before_token["remain_quota"] - amount, (before_token, after_token)
    assert after_token["used_quota"] == before_token["used_quota"] + amount, (before_token, after_token)
    return after_user, after_token


def test_relay_forwards_request_and_returns_upstream_reply(relay, stubs, tag):
    a = relay.user("vip")
    token = relay.create_token(a, name="main")
    before = _accounts(relay, a, token)
    status, reply = relay.chat(token["key"], f"hello {tag}")
    assert status == 200, reply
    same(reply, stubs.reply("vip", MODEL, (100, 50)))
    calls = stubs.tagged(tag)
    assert len(calls) == 1, calls
    call = calls[0]
    assert call["upstream"] == "vip" and call["path"] == "/v1/chat/completions"
    assert call["auth"] == "Bearer sk-upstream-vip"
    assert call["body"]["model"] == MODEL
    assert call["body"]["messages"] == [{"role": "user", "content": f"hello {tag}"}]
    assert not call["body"].get("stream")
    _charged(relay, a, token, *before, 63)
    entries = relay.logs(a)
    assert len(entries) == 1, entries
    entry = entries[0]
    for field, value in (("user_id", a["id"]), ("type", 2), ("username", a["username"]), ("token_name", "main"),
                         ("model_name", MODEL), ("quota", 63), ("prompt_tokens", 100), ("completion_tokens", 50),
                         ("channel", relay.channel_ids["vip"])):
        same(entry[field], value, field)


@pytest.mark.parametrize(("prompt", "completion", "charge"), [(10, 5, 7), (3, 1, 2), (1, 0, 1), (400, 200, 250)])
def test_charge_follows_reported_usage(relay, stubs, tag, prompt, completion, charge):
    a = relay.user("vip")
    token = relay.create_token(a, name="usage")
    before = _accounts(relay, a, token)
    status, reply = relay.chat(token["key"], f"#usage:{prompt}/{completion}# {tag}")
    assert status == 200, reply
    number(reply["usage"]["prompt_tokens"], prompt)
    number(reply["usage"]["completion_tokens"], completion)
    _charged(relay, a, token, *before, charge)
    [entry] = relay.logs(a)
    number(entry["quota"], charge, "log quota")
    number(entry["prompt_tokens"], prompt, "log prompt_tokens")
    number(entry["completion_tokens"], completion, "log completion_tokens")


@pytest.mark.parametrize(("prompt", "completion", "charge"), [(100, 50, 125), (10, 5, 13)])
def test_group_ratio_multiplies_charge(relay, stubs, tag, prompt, completion, charge):
    a = relay.user("double")
    token = relay.create_token(a, name="dbl")
    before = _accounts(relay, a, token)
    status, reply = relay.chat(token["key"], f"#usage:{prompt}/{completion}# {tag}")
    assert status == 200, reply
    assert [c["upstream"] for c in stubs.tagged(tag)] == ["dbl"]
    _charged(relay, a, token, *before, charge)


def test_unlimited_token_charges_only_the_user(relay, stubs, tag):
    a = relay.user("vip")
    token = relay.create_token(a, name="unl", remain_quota=0, unlimited_quota=True)
    before_user, before_token = _accounts(relay, a, token)
    status, reply = relay.chat(token["key"], tag)
    assert status == 200, reply
    relay.wait_for(lambda: relay.user_row(a["id"])["used_quota"] == 63)
    after_user, after_token = _accounts(relay, a, token)
    assert (after_user["quota"], after_user["used_quota"], after_user["request_count"]) == (1_000_000 - 63, 63, 1)
    assert after_token == before_token == {"remain_quota": 0, "used_quota": 0, "status": 1}


def test_authorization_header_forms(relay, stubs, tag):
    a = relay.user("vip")
    token = relay.create_token(a, name="forms")
    for auth in (f"sk-{token['key']}", f"Bearer {token['key']}", token["key"]):
        status, reply = relay.chat(token["key"], tag, auth=auth)
        assert status == 200, (auth, reply)
    assert len(stubs.tagged(tag)) == 3
    relay.wait_for(lambda: relay.user_row(a["id"])["request_count"] == 3)
    assert relay.user_row(a["id"])["used_quota"] == 189

    # Text after a '-' in the key names a specific channel, which ordinary customers may not do.
    status, reply = relay.chat(token["key"], tag, auth=f"Bearer sk-{token['key']}-{relay.channel_ids['vip']}")
    relay_error(status, reply, 403)
    assert len(stubs.tagged(tag)) == 3


def test_rejected_credentials(relay, stubs, tag):
    a = relay.user("vip")
    token = relay.create_token(a, name="real")
    for auth in (None, "Bearer sk-not-a-real-key", f"Bearer sk-{a['access']}", "Bearer sk-" + "A" * 48):
        if auth is None:
            status, reply = relay.call("POST", "/v1/chat/completions", {"model": MODEL, "messages": [{"role": "user", "content": tag}]})
        else:
            status, reply = relay.chat(token["key"], tag, auth=auth)
        relay_error(status, reply, 401)
    assert stubs.tagged(tag) == []
    assert relay.user_row(a["id"]) == {"quota": 1_000_000, "used_quota": 0, "request_count": 0, "status": 1}
    assert relay.logs(a) == []


@pytest.mark.parametrize(("label", "fields", "status_set"), [
    ("disabled", {}, 2),
    ("expired-status", {}, 3),
    ("exhausted-status", {}, 4),
    ("past-expiry", {"expired_time": 1000}, None),
    ("no-quota", {"remain_quota": 0}, None),
    ("negative-quota", {"remain_quota": -5}, None),
])
def test_unusable_token_states_are_refused(relay, stubs, tag, label, fields, status_set):
    a = relay.user("vip")
    token = relay.create_token(a, name=label, **fields)
    if status_set is not None:
        relay.q("UPDATE tokens SET status = %s WHERE id = %s", status_set, token["id"])
    snapshot = relay.token_row(token["id"])
    status, reply = relay.chat(token["key"], tag)
    relay_error(status, reply, 401)
    assert stubs.tagged(tag) == []
    # With the cache enabled the refusal does not rewrite the token's stored status.
    assert relay.token_row(token["id"]) == snapshot
    assert relay.user_row(a["id"]) == {"quota": 1_000_000, "used_quota": 0, "request_count": 0, "status": 1}


def test_model_allowlist(relay, stubs, tag):
    a = relay.user("vip")
    cases = [("gpt-4", 403), ("gpt-4,gpt-3.5-turbo", 200), ("gpt-4, gpt-3.5-turbo", 403), ("gpt-3.5-turbo", 200), ("", 200)]
    for index, (models, expected) in enumerate(cases):
        token = relay.create_token(a, name="allow", models=models)
        marker = f"{tag}-case{index}#"
        status, reply = relay.chat(token["key"], marker)
        if expected == 200:
            assert status == 200, (models, reply)
        else:
            relay_error(status, reply, expected)
        assert len(stubs.tagged(marker)) == (1 if expected == 200 else 0), models
    relay.wait_for(lambda: relay.user_row(a["id"])["request_count"] == 3)
    assert relay.user_row(a["id"])["used_quota"] == 3 * 63


def test_token_subnet_restriction(relay, stubs, tag):
    a = relay.user("vip")
    outside = relay.create_token(a, name="outside", subnet="10.0.0.0/8")
    inside = relay.create_token(a, name="inside", subnet="10.0.0.0/8,127.0.0.0/8")
    status, reply = relay.chat(outside["key"], tag)
    relay_error(status, reply, 403)
    assert stubs.tagged(tag) == []
    status, reply = relay.chat(inside["key"], tag)
    assert status == 200, reply
    assert len(stubs.tagged(tag)) == 1


def test_malformed_requests_are_refused_before_upstream(relay, stubs, tag):
    a = relay.user("vip")
    token = relay.create_token(a, name="bad")
    status, reply = relay.call("POST", "/v1/chat/completions", {"model": MODEL, "note": tag}, auth=f"Bearer sk-{token['key']}")
    relay_error(status, reply, 400)
    status, reply = relay.call("POST", "/v1/chat/completions", raw=b'{"model": "' + tag.encode() + b'", ', auth=f"Bearer sk-{token['key']}")
    relay_error(status, reply, 400)
    status, reply = relay.chat(token["key"], tag, model="gpt-4")
    relay_error(status, reply, 503)
    assert stubs.tagged(tag) == []
    assert relay.user_row(a["id"]) == {"quota": 1_000_000, "used_quota": 0, "request_count": 0, "status": 1}
    assert relay.token_row(token["id"])["used_quota"] == 0


BALANCES = {
    # user quota, token remain_quota, token expired_time
    "ample": (1_000_000, 500_000, -1),
    # Close enough to the request estimate that it is reserved before forwarding and must be returned on failure.
    "low": (10_000, 5_000, 4102444800),
}


@pytest.mark.parametrize(("upstream_status", "balance"), [(500, "ample"), (429, "ample"), (400, "ample"), (500, "low"), (429, "low")])
def test_upstream_failure_is_relayed_without_charge(relay, stubs, tag, upstream_status, balance):
    quota, remain, expires = BALANCES[balance]
    a = relay.user("vip", quota=quota)
    token = relay.create_token(a, name="fail", remain_quota=remain, expired_time=expires)
    status, reply = relay.chat(token["key"], f"#status:{upstream_status}# {tag}")
    relay_error(status, reply, upstream_status, error_type="server_error", code="boom")
    assert len(stubs.tagged(tag)) == 1
    untouched = {"quota": quota, "used_quota": 0, "request_count": 0, "status": 1}
    relay.wait_for(lambda: relay.user_row(a["id"]) == untouched and relay.token_row(token["id"])["remain_quota"] == remain)
    assert relay.user_row(a["id"]) == untouched
    row = relay.token_row(token["id"])
    assert (row["remain_quota"], row["used_quota"], row["status"]) == (remain, 0, 1), row
    assert relay.logs(a) == []

    ok_status, _ = relay.chat(token["key"], f"after failure {tag}")
    assert ok_status == 200
    relay.wait_for(lambda: relay.user_row(a["id"])["request_count"] == 1 and relay.token_row(token["id"])["used_quota"] == 63)
    assert relay.user_row(a["id"]) == {"quota": quota - 63, "used_quota": 63, "request_count": 1, "status": 1}
    row = relay.token_row(token["id"])
    assert (row["remain_quota"], row["used_quota"]) == (remain - 63, 63)
    assert len(relay.logs(a)) == 1


@pytest.mark.parametrize("quota", [0, 100, -50])
def test_insufficient_user_quota(relay, stubs, tag, quota):
    a = relay.user("vip", quota=quota)
    token = relay.create_token(a, name="poor")
    status, reply = relay.chat(token["key"], tag)
    relay_error(status, reply, 403, code="insufficient_user_quota")
    assert stubs.tagged(tag) == []
    assert relay.user_row(a["id"]) == {"quota": quota, "used_quota": 0, "request_count": 0, "status": 1}
    row = relay.token_row(token["id"])
    assert (row["remain_quota"], row["used_quota"], row["status"]) == (500_000, 0, 1)
    assert relay.kv.get(f"user_quota:{a['id']}") == str(quota)
    assert relay.logs(a) == []


def test_low_balance_reserves_token_quota_first(relay, stubs, tag):
    # A user whose balance is not far above the request's estimate has the estimate reserved on the token.
    a = relay.user("vip", quota=10_000)
    small = relay.create_token(a, name="small", remain_quota=100)
    status, reply = relay.chat(small["key"], tag)
    relay_error(status, reply, 403, code="pre_consume_token_quota_failed")
    assert stubs.tagged(tag) == []
    assert relay.user_row(a["id"]) == {"quota": 10_000, "used_quota": 0, "request_count": 0, "status": 1}
    row = relay.token_row(small["id"])
    assert (row["remain_quota"], row["used_quota"], row["status"]) == (100, 0, 1)

    big = relay.create_token(a, name="big", remain_quota=5_000)
    status, reply = relay.chat(big["key"], f"second {tag}")
    assert status == 200, reply
    relay.wait_for(lambda: relay.user_row(a["id"])["request_count"] == 1)
    relay.wait_for(lambda: relay.token_row(big["id"])["used_quota"] == 63)
    assert relay.user_row(a["id"]) == {"quota": 10_000 - 63, "used_quota": 63, "request_count": 1, "status": 1}
    row = relay.token_row(big["id"])
    assert (row["remain_quota"], row["used_quota"]) == (5_000 - 63, 63)
    assert relay.token_row(small["id"])["remain_quota"] == 100


def test_ample_balance_skips_token_reservation(relay, stubs, tag):
    # Counterpart of the low-balance case: a user far above the estimate is trusted, so a token whose
    # remaining quota is below the estimate (but positive) is served and may be overdrawn.
    a = relay.user("vip", quota=1_000_000)
    tiny = relay.create_token(a, name="tiny", remain_quota=10)
    some = relay.create_token(a, name="some", remain_quota=100)
    for token in (tiny, some):
        status, reply = relay.chat(token["key"], f"{token['name']} {tag}")
        assert status == 200, reply
    assert len(stubs.tagged(tag)) == 2
    relay.wait_for(lambda: relay.user_row(a["id"])["request_count"] == 2)
    relay.wait_for(lambda: relay.token_row(tiny["id"])["used_quota"] == 63 and relay.token_row(some["id"])["used_quota"] == 63)
    assert relay.user_row(a["id"]) == {"quota": 1_000_000 - 126, "used_quota": 126, "request_count": 2, "status": 1}
    for token, remain in ((tiny, -53), (some, 37)):
        row = relay.token_row(token["id"])
        assert (row["remain_quota"], row["used_quota"], row["status"]) == (remain, 63, 1), row

    # Once the cached token copies expire, the overdrawn token is refused and the positive one still works.
    time.sleep(SYNC + 1.5)
    status, reply = relay.chat(tiny["key"], f"late tiny {tag}")
    relay_error(status, reply, 401)
    status, reply = relay.chat(some["key"], f"late some {tag}")
    assert status == 200, reply
    assert [c["upstream"] for c in stubs.tagged(f"late tiny {tag}")] == []
    assert len(stubs.tagged(f"late some {tag}")) == 1
    relay.wait_for(lambda: relay.token_row(some["id"])["used_quota"] == 126)
    row = relay.token_row(some["id"])
    assert (row["remain_quota"], row["used_quota"]) == (-26, 126), row
    row = relay.token_row(tiny["id"])
    assert (row["remain_quota"], row["used_quota"], row["status"]) == (-53, 63, 1), row


def test_disabled_user_is_refused(relay, stubs, tag):
    a = relay.user("vip")
    token = relay.create_token(a, name="soon-disabled")
    relay.q("UPDATE users SET status = 2 WHERE id = %s", a["id"])
    status, reply = relay.chat(token["key"], tag)
    relay_error(status, reply, 403)
    assert stubs.tagged(tag) == []
    assert relay.kv.get(f"user_enabled:{a['id']}") == "0"
    assert relay.user_row(a["id"]) == {"quota": 1_000_000, "used_quota": 0, "request_count": 0, "status": 2}
