"""Model listing for relay keys: GET /v1/models and GET /v1/models/{model}."""

from checks import relay_error, same
from conftest import MULTI_MODELS


def _models(relay, key):
    status, reply = relay.call("GET", "/v1/models", auth=f"Bearer sk-{key}")
    assert status == 200, reply
    assert reply.get("object") == "list" and isinstance(reply.get("data"), list), reply
    return reply["data"]


def _retrieve(relay, key, model):
    status, reply = relay.call("GET", f"/v1/models/{model}", auth=f"Bearer sk-{key}")
    assert status == 200, reply
    return reply


def test_models_list_the_callers_group(relay):
    user = relay.user("multi")
    token = relay.create_token(user, name="list")
    ids = [entry["id"] for entry in _models(relay, token["key"])]
    assert sorted(ids) == sorted(MULTI_MODELS.split(",")) and len(ids) == len(set(ids)), ids


def test_builtin_models_come_before_custom_ones(relay):
    user = relay.user("multi")
    token = relay.create_token(user, name="order")
    ids = [entry["id"] for entry in _models(relay, token["key"])]
    assert ids[-1] == "custom-model", ids
    assert ids.index("gpt-3.5-turbo") < ids.index("gpt-4") < ids.index("text-embedding-ada-002") < ids.index("dall-e-2"), ids


def test_listed_builtin_model_matches_retrieved_model(relay):
    user = relay.user("multi")
    token = relay.create_token(user, name="shape")
    listed = {entry["id"]: entry for entry in _models(relay, token["key"])}
    for model in ("gpt-4", "dall-e-3", "tts-1"):
        retrieved = _retrieve(relay, token["key"], model)
        same(listed[model], retrieved, model)
        for field, value in (("id", model), ("object", "model"), ("created", 1626777600), ("owned_by", "openai"), ("root", model)):
            same(retrieved[field], value, f"{model}.{field}")
        assert retrieved["parent"] is None and isinstance(retrieved["permission"], list) and retrieved["permission"], retrieved


def test_custom_model_entry(relay):
    user = relay.user("multi")
    token = relay.create_token(user, name="custom")
    [entry] = [entry for entry in _models(relay, token["key"]) if entry["id"] == "custom-model"]
    same(entry, {"id": "custom-model", "object": "model", "created": 1626777600, "owned_by": "custom", "permission": None,
                 "root": "custom-model", "parent": None})


def test_token_allowlist_replaces_group_models(relay):
    user = relay.user("vip")
    token = relay.create_token(user, name="allow", models="gpt-4,custom-model,whisper-1")
    entries = _models(relay, token["key"])
    assert sorted(entry["id"] for entry in entries) == ["custom-model", "gpt-4", "whisper-1"]
    owners = {entry["id"]: entry["owned_by"] for entry in entries}
    assert owners == {"gpt-4": "openai", "whisper-1": "openai", "custom-model": "custom"}


def test_group_without_channels_lists_no_models(relay):
    user = relay.user("nochan")
    token = relay.create_token(user, name="empty")
    assert _models(relay, token["key"]) == []


def test_retrieve_any_builtin_model_regardless_of_group(relay):
    user = relay.user("vip")
    token = relay.create_token(user, name="any", models="gpt-3.5-turbo")
    reply = _retrieve(relay, token["key"], "dall-e-2")
    assert reply["id"] == "dall-e-2" and reply["owned_by"] == "openai", reply


def test_retrieve_unknown_model_reports_model_not_found(relay):
    user = relay.user("multi")
    token = relay.create_token(user, name="unknown")
    for model in ("custom-model", "no-such-model"):
        reply = _retrieve(relay, token["key"], model)
        assert set(reply) == {"error"}, reply
        error = reply["error"]
        assert (error["type"], error["param"], error["code"]) == ("invalid_request_error", "model", "model_not_found"), error
        assert isinstance(error["message"], str) and error["message"]


def test_model_routes_need_a_valid_key(relay):
    user = relay.user("vip")
    token = relay.create_token(user, name="gone")
    relay.q("UPDATE tokens SET status = 2 WHERE id = %s", token["id"])
    for auth in (None, "Bearer sk-" + "B" * 48, f"Bearer sk-{token['key']}"):
        for path in ("/v1/models", "/v1/models/gpt-4"):
            status, reply = relay.call("GET", path, auth=auth)
            relay_error(status, reply, 401)
