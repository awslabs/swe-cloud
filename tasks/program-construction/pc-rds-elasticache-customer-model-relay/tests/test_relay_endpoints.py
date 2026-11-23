"""Relay endpoints beyond plain chat: completions, embeddings, moderations, images, speech, streaming and unimplemented routes.

Users of group `multi` (ratio 1) and `multi3` (ratio 3) reach the `multi` channel, which serves several model kinds.
"""

import json
import urllib.request

import pytest

from checks import number, relay_error, same

MULTI = "multi"


def _setup(relay, group=MULTI, quota=1_000_000, **token_fields):
    user = relay.user(group, quota=quota)
    token = relay.create_token(user, name="ep", **token_fields)
    return user, token


def _post(relay, token, path, body):
    return relay.call("POST", path, body, auth=f"Bearer sk-{token['key']}")


def _charged(relay, user, token, amount):
    relay.wait_for(lambda: relay.user_row(user["id"])["request_count"] == 1)
    relay.wait_for(lambda: relay.token_row(token["id"])["used_quota"] == amount)
    row = relay.user_row(user["id"])
    assert (row["used_quota"], row["request_count"]) == (amount, 1), row
    assert row["quota"] == 1_000_000 - amount, row
    assert relay.token_row(token["id"])["remain_quota"] == 500_000 - amount


def _untouched(relay, user, token, quota=1_000_000):
    assert relay.user_row(user["id"]) == {"quota": quota, "used_quota": 0, "request_count": 0, "status": 1}
    assert relay.token_row(token["id"])["used_quota"] == 0


def test_text_completion_forwarded_and_priced(relay, stubs, tag):
    user, token = _setup(relay)
    status, reply = _post(relay, token, "/v1/completions", {"model": "text-davinci-003", "prompt": f"#usage:100/50# {tag}"})
    assert status == 200, reply
    same(reply, stubs.text_reply("multi", "text-davinci-003", (100, 50)))
    [call] = stubs.tagged(tag)
    assert (call["path"], call["auth"], call["body"]["prompt"]) == ("/v1/completions", "Bearer sk-upstream-multi", f"#usage:100/50# {tag}")
    _charged(relay, user, token, 1500)
    [entry] = relay.logs(user)
    for field, value in (("model_name", "text-davinci-003"), ("quota", 1500), ("prompt_tokens", 100), ("completion_tokens", 50),
                         ("channel", relay.channel_ids["multi"]), ("type", 2)):
        same(entry[field], value, field)


def test_chat_price_depends_on_model(relay, stubs, tag):
    user, token = _setup(relay)
    status, reply = relay.chat(token["key"], f"#usage:100/50# {tag}", model="gpt-4")
    assert status == 200, reply
    assert [c["body"]["model"] for c in stubs.tagged(tag)] == ["gpt-4"]
    _charged(relay, user, token, 3000)


def test_embeddings_priced_from_prompt_tokens(relay, stubs, tag):
    user, token = _setup(relay)
    status, reply = _post(relay, token, "/v1/embeddings", {"model": "text-embedding-ada-002", "input": f"#usage:400/0# {tag}"})
    assert status == 200, reply
    same(reply, {"object": "list", "model": "text-embedding-ada-002",
                 "data": [{"object": "embedding", "index": 0, "embedding": [0.5, -0.25]}],
                 "usage": {"prompt_tokens": 400, "total_tokens": 400}})
    assert [c["path"] for c in stubs.tagged(tag)] == ["/v1/embeddings"]
    _charged(relay, user, token, 20)
    [entry] = relay.logs(user)
    number(entry["prompt_tokens"], 400, "prompt_tokens")
    number(entry["completion_tokens"], 0, "completion_tokens")


def test_moderation_priced_from_prompt_tokens(relay, stubs, tag):
    user, token = _setup(relay)
    status, reply = _post(relay, token, "/v1/moderations", {"model": "text-moderation-latest", "input": f"#usage:40/0# {tag}"})
    assert status == 200, reply
    assert reply["results"] == [{"flagged": False}] and reply["id"] == "modr-multi", reply
    assert [c["path"] for c in stubs.tagged(tag)] == ["/v1/moderations"]
    _charged(relay, user, token, 4)


def test_group_ratio_multiplies_the_price(relay, stubs, tag):
    user, token = _setup(relay, group="multi3")
    status, reply = relay.chat(token["key"], f"#usage:100/50# {tag}", model="gpt-4")
    assert status == 200, reply
    _charged(relay, user, token, 9000)


@pytest.mark.parametrize(("model", "size", "n", "quality", "charge"), [
    ("dall-e-2", "256x256", 1, None, 10_000),
    ("dall-e-2", "512x512", 2, None, 22_500),
    ("dall-e-2", None, 3, None, 37_500),
    ("dall-e-3", "1024x1024", 1, "hd", 40_000),
    ("dall-e-3", "1792x1024", 1, "standard", 40_000),
    ("dall-e-3", "1024x1792", 1, "hd", 60_000),
])
def test_image_generation_priced_by_model_size_count_and_quality(relay, stubs, tag, model, size, n, quality, charge):
    user, token = _setup(relay)
    body = {"model": model, "prompt": f"a lighthouse {tag}", "n": n}
    if size:
        body["size"] = size
    if quality:
        body["quality"] = quality
    status, reply = _post(relay, token, "/v1/images/generations", body)
    assert status == 200, reply
    same(reply, {"created": 1700000000, "data": [{"url": "https://images.invalid/multi.png"}]})
    [call] = stubs.tagged(tag)
    assert call["path"] == "/v1/images/generations" and call["body"]["model"] == model and call["body"]["n"] == n, call
    _charged(relay, user, token, charge)
    [entry] = relay.logs(user)
    for field, value in (("model_name", model), ("quota", charge), ("prompt_tokens", 0), ("completion_tokens", 0)):
        same(entry[field], value, field)


@pytest.mark.parametrize(("body", "code"), [
    ({"model": "dall-e-3", "size": "512x512"}, "size_not_supported"),
    ({"model": "dall-e-3", "n": 2}, "n_not_within_range"),
    ({"model": "dall-e-2", "n": 11}, "n_not_within_range"),
    ({"model": "dall-e-2", "prompt": ""}, "prompt_missing"),
])
def test_invalid_image_requests_refused_before_upstream(relay, stubs, tag, body, code):
    user, token = _setup(relay)
    status, reply = _post(relay, token, "/v1/images/generations", {"prompt": f"a lighthouse {tag}", **body})
    relay_error(status, reply, 400, code=code)
    assert stubs.tagged(tag) == []
    _untouched(relay, user, token)


def test_image_needs_enough_user_quota(relay, stubs, tag):
    user, token = _setup(relay, quota=30_000)
    status, reply = _post(relay, token, "/v1/images/generations", {"model": "dall-e-3", "prompt": tag, "quality": "hd"})
    relay_error(status, reply, 403, code="insufficient_user_quota")
    assert stubs.tagged(tag) == []
    _untouched(relay, user, token, quota=30_000)


def test_speech_returns_audio_and_is_priced_by_input_length(relay, stubs, tag):
    user, token = _setup(relay)
    request = urllib.request.Request(relay.base + "/v1/audio/speech", method="POST",
                                     data=json.dumps({"model": "tts-1", "input": "hello world", "voice": tag}).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer sk-{token['key']}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        assert response.status == 200
        assert response.headers.get("Content-Type", "").startswith("audio/mpeg"), dict(response.headers)
        assert response.read() == stubs.audio("multi")
    [call] = stubs.tagged(tag)
    assert call["path"] == "/v1/audio/speech" and call["body"]["input"] == "hello world"
    _charged(relay, user, token, 82)
    [entry] = relay.logs(user)
    for field, value in (("model_name", "tts-1"), ("quota", 82), ("prompt_tokens", 82), ("completion_tokens", 0)):
        same(entry[field], value, field)


def test_streamed_chat_relays_events_and_charges_reported_usage(relay, stubs, tag):
    user, token = _setup(relay, group="vip")
    body = {"model": "gpt-3.5-turbo", "stream": True, "messages": [{"role": "user", "content": f"#usage:100/50# {tag}"}]}
    request = urllib.request.Request(relay.base + "/v1/chat/completions", data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer sk-{token['key']}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        assert response.status == 200
        assert response.headers.get("Content-Type", "").startswith("text/event-stream"), dict(response.headers)
        text = response.read().decode()
    payloads = [line[len("data: "):] for line in text.splitlines() if line.startswith("data: ")]
    assert payloads[-1] == "[DONE]", payloads
    same([json.loads(p) for p in payloads[:-1]], stubs.stream_events("vip", "gpt-3.5-turbo", (100, 50)))
    [call] = stubs.tagged(tag)
    assert call["body"]["stream"] is True
    _charged(relay, user, token, 63)
    [entry] = relay.logs(user)
    number(entry["quota"], 63, "log quota")


@pytest.mark.parametrize(("method", "path"), [
    ("POST", "/v1/images/edits"), ("POST", "/v1/images/variations"), ("POST", "/v1/files"),
    ("POST", "/v1/fine_tuning/jobs"), ("POST", "/v1/assistants"), ("POST", "/v1/threads"),
])
def test_unimplemented_endpoints_answer_501(relay, stubs, tag, method, path):
    user, token = _setup(relay, group="vip")
    status, reply = relay.call(method, path, {"model": "gpt-3.5-turbo", "note": tag}, auth=f"Bearer sk-{token['key']}")
    relay_error(status, reply, 501, code="api_not_implemented")
    assert stubs.tagged(tag) == []
    _untouched(relay, user, token)
