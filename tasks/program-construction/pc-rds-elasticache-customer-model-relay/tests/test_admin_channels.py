"""Administrator channel management: /api/channel/ create, read, search, update, delete and test, with their routing effect."""

import secrets
import socket
import time

import pytest

from checks import failed_envelope, ok_envelope, relay_error
from conftest import SYNC, Stubs

AFTER_SYNC = SYNC + 2
CHANNEL_FIELDS = {"id", "type", "key", "status", "name", "weight", "created_time", "test_time", "response_time", "base_url", "other",
                  "balance", "balance_updated_time", "models", "group", "used_quota", "model_mapping", "priority", "config",
                  "system_prompt"}


@pytest.fixture(scope="module")
def own_stubs():
    servers = Stubs(["adm-a", "adm-b"])
    yield servers
    servers.close()


def _closed_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _admin(relay):
    return relay.user("default", role=10)


def _group():
    return "c" + secrets.token_hex(4)


def _create(relay, admin, **fields):
    body = {"type": 1, "key": "sk-made", "name": "made-" + secrets.token_hex(3), "base_url": "http://127.0.0.1:9",
            "models": "gpt-3.5-turbo", "group": _group(), "priority": 0, "weight": 0, **fields}
    status, reply = relay.api(admin, "POST", "/api/channel/", body)
    assert status == 200 and reply["success"] is True, reply
    return body, relay.rows('SELECT * FROM channels WHERE name = %s ORDER BY id', body["name"])


def _abilities(relay, channel_id):
    return relay.rows('SELECT "group", model, enabled, priority FROM abilities WHERE channel_id = %s ORDER BY "group", model', channel_id)


def test_create_channel_stores_row_and_abilities(relay, own_stubs):
    admin = _admin(relay)
    group = _group()
    before = int(time.time()) - 1
    body, [row] = _create(relay, admin, key="sk-one", base_url=own_stubs.urls["adm-a"], models="gpt-3.5-turbo,tts-1",
                          group=f"{group},{group}x", priority=3, weight=2)
    for field in ("type", "key", "name", "base_url", "models", "group", "priority", "weight"):
        assert row[field] == body[field], field
    assert row["status"] == 1 and row["used_quota"] == 0 and before <= row["created_time"] <= int(time.time()) + 1, row
    assert _abilities(relay, row["id"]) == [
        {"group": group, "model": "gpt-3.5-turbo", "enabled": True, "priority": 3},
        {"group": group, "model": "tts-1", "enabled": True, "priority": 3},
        {"group": f"{group}x", "model": "gpt-3.5-turbo", "enabled": True, "priority": 3},
        {"group": f"{group}x", "model": "tts-1", "enabled": True, "priority": 3},
    ]


def test_create_with_several_keys_makes_one_channel_per_key(relay):
    admin = _admin(relay)
    _, rows = _create(relay, admin, key="sk-first\nsk-second\n\nsk-third")
    assert [row["key"] for row in rows] == ["sk-first", "sk-second", "sk-third"]
    assert len({row["id"] for row in rows}) == 3
    for row in rows:
        assert len(_abilities(relay, row["id"])) == 1


def test_create_channel_refuses_malformed_body(relay):
    admin = _admin(relay)
    name = "bad-" + secrets.token_hex(3)
    status, reply = relay.api(admin, "POST", "/api/channel/", {"type": "openai", "key": "k", "name": name})
    failed_envelope(reply, status)
    assert relay.rows("SELECT id FROM channels WHERE name = %s", name) == []


def test_get_channel_hides_key(relay):
    admin = _admin(relay)
    _, [row] = _create(relay, admin, key="sk-secret")
    status, reply = relay.api(admin, "GET", f"/api/channel/{row['id']}")
    data = ok_envelope(status, reply)
    assert set(data) == CHANNEL_FIELDS, sorted(data)
    assert data["key"] == ""
    for field in ("id", "type", "status", "name", "base_url", "models", "group", "priority", "weight", "created_time"):
        assert data[field] == row[field], field
    status, reply = relay.api(admin, "GET", "/api/channel/999999")
    failed_envelope(reply, status)


def test_list_channels_newest_first_without_keys(relay):
    admin = _admin(relay)
    _, [first] = _create(relay, admin)
    _, [second] = _create(relay, admin)
    status, reply = relay.api(admin, "GET", "/api/channel/?p=0")
    data = ok_envelope(status, reply)
    assert [c["id"] for c in data[:2]] == [second["id"], first["id"]]
    assert all(c["key"] == "" for c in data)
    ids = [c["id"] for c in data]
    assert ids == sorted(ids, reverse=True)


def test_search_channels_by_name_prefix_or_id(relay):
    admin = _admin(relay)
    stem = "srch" + secrets.token_hex(3)
    _, [one] = _create(relay, admin, name=f"{stem}-one")
    _, [two] = _create(relay, admin, name=f"{stem}-two")
    status, reply = relay.api(admin, "GET", f"/api/channel/search?keyword={stem}")
    found = ok_envelope(status, reply)
    assert sorted(c["id"] for c in found) == sorted([one["id"], two["id"]])
    assert all(c["key"] == "" for c in found)
    status, reply = relay.api(admin, "GET", f"/api/channel/search?keyword={two['id']}")
    assert [c["id"] for c in ok_envelope(status, reply)] == [two["id"]]


def test_update_rewrites_channel_and_abilities(relay):
    admin = _admin(relay)
    group = _group()
    _, [row] = _create(relay, admin, models="gpt-3.5-turbo,tts-1", group=f"{group},{group}x", priority=3)
    status, reply = relay.api(admin, "PUT", "/api/channel/", {"id": row["id"], "type": 1, "name": "renamed", "models": "gpt-4",
                                                               "group": group, "base_url": row["base_url"], "priority": 7, "status": 2})
    data = ok_envelope(status, reply)
    assert (data["name"], data["models"], data["group"], data["priority"], data["status"]) == ("renamed", "gpt-4", group, 7, 2)
    stored = relay.rows('SELECT name, models, "group", priority, status, key FROM channels WHERE id = %s', row["id"])[0]
    assert stored == {"name": "renamed", "models": "gpt-4", "group": group, "priority": 7, "status": 2, "key": row["key"]}
    assert _abilities(relay, row["id"]) == [{"group": group, "model": "gpt-4", "enabled": False, "priority": 7}]


def test_update_with_only_some_fields_keeps_the_rest(relay):
    admin = _admin(relay)
    _, [row] = _create(relay, admin, models="gpt-3.5-turbo", priority=4, weight=9)
    status, reply = relay.api(admin, "PUT", "/api/channel/", {"id": row["id"], "name": "only-name"})
    ok_envelope(status, reply)
    stored = relay.rows('SELECT name, models, "group", priority, weight, status, key, base_url FROM channels WHERE id = %s', row["id"])[0]
    assert stored == {"name": "only-name", "models": row["models"], "group": row["group"], "priority": 4, "weight": 9, "status": 1,
                      "key": row["key"], "base_url": row["base_url"]}
    assert _abilities(relay, row["id"]) == [{"group": row["group"], "model": "gpt-3.5-turbo", "enabled": True, "priority": 4}]


def test_created_channel_serves_its_group_after_sync(relay, own_stubs, tag):
    admin = _admin(relay)
    group = _group()
    _create(relay, admin, key="sk-adm-a", base_url=own_stubs.urls["adm-a"], group=group)
    user = relay.user(group)
    token = relay.create_token(user, name="new")
    time.sleep(AFTER_SYNC)
    status, reply = relay.chat(token["key"], tag)
    assert status == 200, reply
    assert [(c["upstream"], c["auth"]) for c in own_stubs.tagged(tag)] == [("adm-a", "Bearer sk-adm-a")]


def test_deleted_channel_stops_serving_after_sync(relay, own_stubs, tag):
    admin = _admin(relay)
    group = _group()
    _, [row] = _create(relay, admin, key="sk-adm-b", base_url=own_stubs.urls["adm-b"], group=group)
    user = relay.user(group)
    token = relay.create_token(user, name="doomed")
    time.sleep(AFTER_SYNC)
    assert relay.chat(token["key"], f"before {tag}")[0] == 200
    status, reply = relay.api(admin, "DELETE", f"/api/channel/{row['id']}")
    ok_envelope(status, reply)
    assert relay.rows("SELECT id FROM channels WHERE id = %s", row["id"]) == []
    assert _abilities(relay, row["id"]) == []
    time.sleep(AFTER_SYNC)
    status, reply = relay.chat(token["key"], f"after {tag}")
    relay_error(status, reply, 503)
    assert own_stubs.tagged(f"after {tag}") == []


def test_channel_test_sends_a_probe_chat_request(relay, own_stubs):
    admin = _admin(relay)
    key = "sk-probe-" + secrets.token_hex(4)
    _, [row] = _create(relay, admin, key=key, base_url=own_stubs.urls["adm-b"])
    before = int(time.time()) - 1
    status, reply = relay.api(admin, "GET", f"/api/channel/test/{row['id']}")
    assert status == 200 and reply["success"] is True and reply["model"] == "" and isinstance(reply["time"], (int, float)), reply
    calls = [c for c in own_stubs.calls if c["auth"] == f"Bearer {key}"]
    assert len(calls) == 1 and calls[0]["path"] == "/v1/chat/completions", calls
    assert calls[0]["body"] == {"model": "gpt-3.5-turbo", "max_tokens": 2, "messages": [{"role": "user", "content": "hi"}]}
    relay.wait_for(lambda: (relay.rows("SELECT test_time FROM channels WHERE id = %s", row["id"])[0]["test_time"] or 0) >= before)
    assert relay.rows("SELECT test_time, status FROM channels WHERE id = %s", row["id"])[0]["test_time"] >= before


def test_channel_test_uses_requested_model_only_if_served(relay, own_stubs):
    admin = _admin(relay)
    key = "sk-probe-" + secrets.token_hex(4)
    _, [row] = _create(relay, admin, key=key, base_url=own_stubs.urls["adm-b"], models="gpt-3.5-turbo,gpt-4")
    for requested in ("gpt-4", "dall-e-3"):
        status, reply = relay.api(admin, "GET", f"/api/channel/test/{row['id']}?model={requested}")
        assert status == 200 and reply["success"] is True and reply["model"] == requested, reply
    calls = [c for c in own_stubs.calls if c["auth"] == f"Bearer {key}"]
    assert [c["body"]["model"] for c in calls] == ["gpt-4", "gpt-3.5-turbo"]


def test_channel_test_reports_unreachable_upstream(relay):
    admin = _admin(relay)
    _, [row] = _create(relay, admin, base_url=f"http://127.0.0.1:{_closed_port()}")
    status, reply = relay.api(admin, "GET", f"/api/channel/test/{row['id']}")
    assert status == 200 and reply["success"] is False and reply["message"], reply
    assert relay.rows("SELECT status FROM channels WHERE id = %s", row["id"])[0]["status"] == 1


def test_channel_routes_need_an_administrator(relay):
    customer = relay.user("vip")
    admin = _admin(relay)
    _, [row] = _create(relay, admin)
    for method, path, body in (("GET", "/api/channel/?p=0", None), ("GET", f"/api/channel/{row['id']}", None),
                               ("POST", "/api/channel/", {"type": 1, "key": "k", "name": "sneaky", "group": "vip", "models": "gpt-4"}),
                               ("DELETE", f"/api/channel/{row['id']}", None)):
        status, reply = relay.api(customer, method, path, body)
        failed_envelope(reply, status)
    status, reply = relay.call("GET", "/api/channel/?p=0")
    failed_envelope(reply, status, http_status=401)
    assert relay.rows("SELECT id FROM channels WHERE id = %s", row["id"]) != []
    assert relay.rows("SELECT id FROM channels WHERE name = 'sneaky'") == []
