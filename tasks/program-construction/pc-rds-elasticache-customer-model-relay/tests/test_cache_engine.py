"""Valkey entries beyond the relay path: group model lists, user groups for model listing, quota reservations and reloads."""

import json
import secrets
import time

import redis

from checks import cached_token, ttl_within
from conftest import KV_DB, MULTI_MODELS, SYNC

AFTER_TTL = SYNC + 1.5


def _group_with_abilities(relay, models, disabled=()):
    """A fresh group whose ability rows (on an existing channel) list `models`; `disabled` rows are not enabled."""
    group = "g" + secrets.token_hex(4)
    channel = relay.channel_ids["vip"]
    for model in models:
        relay.q('INSERT INTO abilities("group", model, channel_id, enabled, priority) VALUES (%s, %s, %s, %s, 0)',
                group, model, channel, model not in disabled)
    return group


def _available(relay, user):
    status, reply = relay.api(user, "GET", "/api/user/available_models")
    assert status == 200 and reply["success"] is True, reply
    return reply["data"]


def test_available_models_lists_group_models_sorted(relay):
    user = relay.user("multi")
    assert _available(relay, user) == sorted(MULTI_MODELS.split(","))


def test_available_models_writes_group_models_entry(relay):
    group = _group_with_abilities(relay, ["zeta-model", "alpha-model", "mid-model", "off-model"], disabled=("off-model",))
    user = relay.user(group)
    assert relay.kv.get(f"group_models:{group}") is None
    assert _available(relay, user) == ["alpha-model", "mid-model", "zeta-model"]
    assert relay.kv.get(f"group_models:{group}") == "alpha-model,mid-model,zeta-model"
    ttl_within(relay, f"group_models:{group}", SYNC)


def test_group_models_entry_reloaded_after_expiry(relay):
    group = _group_with_abilities(relay, ["first-model"])
    user = relay.user(group)
    assert _available(relay, user) == ["first-model"]
    relay.q('INSERT INTO abilities("group", model, channel_id, enabled, priority) VALUES (%s, %s, %s, true, 0)',
            group, "added-model", relay.channel_ids["vip"])
    time.sleep(AFTER_TTL)
    assert relay.kv.get(f"group_models:{group}") is None
    assert _available(relay, user) == ["added-model", "first-model"]
    assert relay.kv.get(f"group_models:{group}") == "added-model,first-model"


def test_v1_models_writes_group_models_entry(relay):
    group = _group_with_abilities(relay, ["beta-model", "alpha-model"])
    user = relay.user(group)
    token = relay.create_token(user, name="lister")
    status, reply = relay.call("GET", "/v1/models", auth=f"Bearer sk-{token['key']}")
    assert status == 200, reply
    assert sorted(entry["id"] for entry in reply["data"]) == ["alpha-model", "beta-model"]
    assert relay.kv.get(f"group_models:{group}") == "alpha-model,beta-model"
    assert relay.kv.get(f"user_group:{user['id']}") == group


def test_model_listing_with_token_allowlist_skips_group_entry(relay):
    group = _group_with_abilities(relay, ["beta-model"])
    user = relay.user(group)
    token = relay.create_token(user, name="lister", models="gamma-model")
    status, reply = relay.call("GET", "/v1/models", auth=f"Bearer sk-{token['key']}")
    assert status == 200, reply
    assert [entry["id"] for entry in reply["data"]] == ["gamma-model"]
    assert relay.kv.get(f"group_models:{group}") is None


def test_available_models_caches_user_group(relay):
    user = relay.user("svip")
    assert relay.kv.get(f"user_group:{user['id']}") is None
    assert _available(relay, user) == ["gpt-3.5-turbo"]
    assert relay.kv.get(f"user_group:{user['id']}") == "svip"
    ttl_within(relay, f"user_group:{user['id']}", SYNC)


def test_speech_reservation_is_subtracted_from_cached_quota(relay, stubs, tag):
    user = relay.user("multi", quota=1_000_000)
    token = relay.create_token(user, name="voice")
    status, _ = relay.call("POST", "/v1/audio/speech", {"model": "tts-1", "input": "x" * 100, "voice": tag},
                           auth=f"Bearer sk-{token['key']}")
    assert status == 200
    relay.wait_for(lambda: relay.user_row(user["id"])["quota"] == 1_000_000 - 750)
    assert relay.kv.get(f"user_quota:{user['id']}") == str(1_000_000 - 750)
    ttl_within(relay, f"user_quota:{user['id']}", SYNC)


def test_cached_quota_reloaded_from_database_after_expiry(relay, stubs, tag):
    user = relay.user("multi", quota=1_000_000)
    token = relay.create_token(user, name="voice")
    auth = f"Bearer sk-{token['key']}"
    assert relay.call("POST", "/v1/audio/speech", {"model": "tts-1", "input": "x" * 100, "voice": tag}, auth=auth)[0] == 200
    relay.wait_for(lambda: relay.user_row(user["id"])["quota"] == 1_000_000 - 750)
    relay.q("UPDATE users SET quota = quota + 5000 WHERE id = %s", user["id"])
    time.sleep(AFTER_TTL)
    assert relay.call("POST", "/v1/audio/speech", {"model": "tts-1", "input": "x" * 10, "voice": tag}, auth=auth)[0] == 200
    expected = 1_000_000 - 750 + 5000 - 75
    relay.wait_for(lambda: relay.user_row(user["id"])["quota"] == expected)
    assert relay.user_row(user["id"])["quota"] == expected
    assert relay.kv.get(f"user_quota:{user['id']}") == str(expected)


def test_token_entry_reloaded_after_expiry(relay, stubs, tag):
    user = relay.user("vip")
    token = relay.create_token(user, name="reload")
    assert relay.chat(token["key"], f"first {tag}")[0] == 200
    assert cached_token(relay, token["key"])["remain_quota"] == 500_000
    relay.wait_for(lambda: relay.token_row(token["id"])["used_quota"] == 63)
    time.sleep(AFTER_TTL)
    assert relay.kv.get(f"token:{token['key']}") is None
    assert relay.chat(token["key"], f"second {tag}")[0] == 200
    cached = cached_token(relay, token["key"])
    assert (cached["remain_quota"], cached["used_quota"]) == (500_000 - 63, 63), cached
    ttl_within(relay, f"token:{token['key']}", SYNC)


def test_entries_live_only_in_the_configured_database(relay, stubs, tag):
    user = relay.user("vip")
    token = relay.create_token(user, name="where")
    assert relay.chat(token["key"], tag)[0] == 200
    names = [f"token:{token['key']}", f"user_quota:{user['id']}", f"user_group:{user['id']}", f"user_enabled:{user['id']}"]
    assert all(relay.kv.exists(name) for name in names)
    pool = relay.kv.connection_pool.connection_kwargs
    for other in {0, 1, KV_DB + 1} - {KV_DB}:
        client = redis.Redis(host=pool["host"], port=pool["port"], db=other, decode_responses=True)
        try:
            assert not any(client.exists(name) for name in names), other
        finally:
            client.close()
    assert json.loads(relay.kv.get(names[0]))["id"] == token["id"]
