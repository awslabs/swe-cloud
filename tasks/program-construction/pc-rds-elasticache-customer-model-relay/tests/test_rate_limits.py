"""Request rate limits kept in Valkey, on a second server started with GLOBAL_API_RATE_LIMIT=API_LIMIT.

Each test starts with that server's Valkey database emptied, so no earlier request counts against the limits.
"""

from conftest import API_LIMIT, MODEL

API_KEY = "rateLimit:GA127.0.0.1"
CRITICAL_KEY = "rateLimit:CT127.0.0.1"


def _status(server):
    return server.call("GET", "/api/status")[0]


def test_api_requests_beyond_the_limit_get_429(limited):
    assert [_status(limited) for _ in range(API_LIMIT)] == [200] * API_LIMIT
    status, _ = limited.call("GET", "/api/status")
    assert status == 429


def test_every_api_route_counts_against_one_limit(limited):
    user = limited.user("vip")
    for index in range(API_LIMIT):
        path = ("/api/status", "/api/user/self", "/api/token/?p=0")[index % 3]
        status, _ = limited.call("GET", path, auth=user["access"])
        assert status == 200, (index, path)
    status, _ = limited.api(user, "GET", "/api/user/self")
    assert status == 429


def test_request_history_is_a_valkey_list_per_client(limited):
    for _ in range(3):
        assert _status(limited) == 200
    assert limited.kv.type(API_KEY) == "list"
    assert limited.kv.llen(API_KEY) == 3
    ttl = limited.kv.ttl(API_KEY)
    assert 1100 < ttl <= 1200, ttl


def test_refused_requests_are_not_recorded(limited):
    for _ in range(API_LIMIT + 4):
        _status(limited)
    assert limited.kv.llen(API_KEY) == API_LIMIT


def test_clearing_the_history_lifts_the_limit(limited):
    for _ in range(API_LIMIT):
        _status(limited)
    assert _status(limited) == 429
    limited.kv.delete(API_KEY)
    assert _status(limited) == 200
    assert limited.kv.llen(API_KEY) == 1


def test_relay_routes_are_not_api_limited(limited, stubs, tag):
    user = limited.user("vip")
    token = limited.create_token(user, name="limited")
    limited.kv.flushdb()
    for _ in range(API_LIMIT):
        _status(limited)
    assert _status(limited) == 429
    status, reply = limited.chat(token["key"], tag)
    assert status == 200, reply
    status, reply = limited.call("GET", "/v1/models", auth=f"Bearer sk-{token['key']}")
    assert status == 200 and [entry["id"] for entry in reply["data"]] == [MODEL], reply
    assert limited.kv.llen(API_KEY) == API_LIMIT


def test_login_attempts_have_their_own_limit_of_twenty(limited):
    body = {"username": "nobody", "password": "wrong-password"}
    for _ in range(20):
        status, reply = limited.call("POST", "/api/user/login", body)
        assert status == 200 and reply["success"] is False, reply
    status, _ = limited.call("POST", "/api/user/login", body)
    assert status == 429
    assert limited.kv.llen(CRITICAL_KEY) == 20
    assert limited.kv.llen(API_KEY) == 21
    assert _status(limited) == 200
