"""Configuration file semantics: environment substitution, default database, limits, upstream failure."""

import socket

import pytest
import websocket

from gw import assert_json, free_port, get, request


@pytest.mark.timeout(600)
def test_environment_variable_values(gateway, db, valkey, key):
    port = free_port()
    env = {"GW_HOST_A": valkey[0], "GW_PORT_A": str(valkey[1]), "GW_DB_A": "6",
           "GW_ROOT_A": f"/GET/{key}:root", "GW_HTTP_A": str(port)}
    g = gateway(env=env, connect_port=port, redis_host="$GW_HOST_A", redis_port="$GW_PORT_A",
                database="$GW_DB_A", default_root="$GW_ROOT_A", http_port="$GW_HTTP_A")
    assert_json(get(g.port, f"/SET/{key}:root/from-env"), {"SET": [True, "OK"]})
    assert db(6).get(f"{key}:root") == b"from-env"
    assert db(0).exists(f"{key}:root") == 0
    assert_json(get(g.port, "/"), {"GET": "from-env"})


def test_only_all_caps_references_are_substituted(gateway, valkey):
    g = gateway(env={"gw_lower": valkey[0]}, redis_host="$gw_lower")
    assert get(g.port, "/PING").status == 503
    g = gateway(redis_host="$GW_NOT_SET_ANYWHERE")
    assert get(g.port, "/PING").status == 503


def test_configured_default_database(gateway, db, key):
    g = gateway(database=3)
    assert_json(get(g.port, f"/SET/{key}:d/three"), {"SET": [True, "OK"]})
    assert db(3).get(f"{key}:d") == b"three" and db(0).exists(f"{key}:d") == 0
    db(0).set(f"{key}:d", "zero")
    assert_json(get(g.port, f"/0/GET/{key}:d"), {"GET": "zero"})
    assert_json(get(g.port, f"/GET/{key}:d"), {"GET": "three"})
    assert_json(get(g.port, f"/8/GET/{key}:d"), {"GET": None})


def test_request_size_limit(gateway, db, key):
    g = gateway(http_max_request_size=4000)
    assert_json(request(g.port, "PUT", f"/SET/{key}:small", body=b"x" * 1000), {"SET": [True, "OK"]})
    assert db(0).strlen(f"{key}:small") == 1000
    r = request(g.port, "PUT", f"/SET/{key}:big", body=b"x" * 50000)
    assert r.status == 413
    r = get(g.port, f"/SET/{key}:url/" + "y" * 20000)
    assert r.status == 413
    assert db(0).exists(f"{key}:big", f"{key}:url") == 0
    # the server keeps serving afterwards
    assert_json(get(g.port, f"/GET/{key}:small.json?x"), {"GET": "x" * 1000})


def test_unreachable_redis_gives_503(gateway):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
    g = gateway(redis_host="127.0.0.1", redis_port=dead)
    for path in ("/PING", "/GET/k.raw", "/GET/k.txt", "/5/GET/k"):
        r = get(g.port, path)
        assert r.status == 503 and r.body == b"", (path, r)


def test_websockets_disabled_refuses_upgrade(gateway, key):
    g = gateway(websockets=False)
    with pytest.raises(websocket.WebSocketBadStatusException) as info:
        websocket.create_connection(f"ws://127.0.0.1:{g.port}/.json", timeout=5)
    assert info.value.status_code == 400
    assert_json(get(g.port, "/PING"), {"PING": [True, "PONG"]})
