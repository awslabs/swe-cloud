"""Access-control rules: client matching, rule order and denial effects."""

import base64

import pytest

from gw import assert_json, get, own_address, request


def basic(credentials, scheme="Basic"):
    return ("Authorization", f"{scheme} " + base64.b64encode(credentials.encode()).decode())


def status(g, path, headers=(), host="127.0.0.1"):
    return get(g.port, path, headers=headers, host=host).status


@pytest.mark.timeout(600)
def test_disabled_command_is_refused_without_side_effects(gateway, db, key):
    g = gateway(acl=[{"disabled": ["SET", "flushdb"]}])
    db(0).set(f"{key}:keep", "orig")
    for path in (f"/SET/{key}:keep/new", f"/set/{key}:keep/new", f"/Set/{key}:other/x", f"/5/SET/{key}:keep/new"):
        r = get(g.port, path)
        assert r.status == 403 and r.body == b"", (path, r)
    r = get(g.port, "/FLUSHDB")
    assert r.status == 403
    assert db(0).get(f"{key}:keep") == b"orig"
    assert db(0).exists(f"{key}:other") == 0 and db(5).exists(f"{key}:keep") == 0
    # names are matched exactly, not by prefix
    assert_json(get(g.port, f"/SETEX/{key}:ex/100/v"), {"SETEX": [True, "OK"]})
    assert_json(get(g.port, f"/GET/{key}:keep"), {"GET": "orig"})


def test_denied_post_and_put_leave_state(gateway, db, key):
    g = gateway(acl=[{"disabled": ["SET", "RPUSH"]}])
    assert request(g.port, "POST", "/", body=f"SET/{key}:p/v".encode()).status == 403
    assert request(g.port, "PUT", f"/RPUSH/{key}:l", body=b"x").status == 403
    assert db(0).exists(f"{key}:p", f"{key}:l") == 0
    assert_json(request(g.port, "PUT", f"/LPUSH/{key}:l", body=b"y"), {"LPUSH": 1})


def test_basic_auth_rule_reenables_command(gateway, db, key):
    g = gateway(acl=[{"disabled": ["SETEX"]}, {"http_basic_auth": "user:password", "enabled": ["SETEX"]}])
    path = f"/SETEX/{key}:e/100/v"
    assert status(g, path) == 403
    assert status(g, path, [basic("user:wrong")]) == 403
    assert status(g, path, [basic("user:password", "Bearer")]) == 403
    assert db(0).exists(f"{key}:e") == 0
    r = get(g.port, path, headers=[basic("user:password")])
    assert_json(r, {"SETEX": [True, "OK"]})
    assert db(0).get(f"{key}:e") == b"v" and 0 < db(0).ttl(f"{key}:e") <= 100
    # the scheme name is case-insensitive
    assert status(g, f"/SETEX/{key}:e2/100/v", [basic("user:password", "basic")]) == 200
    assert db(0).get(f"{key}:e2") == b"v"


def test_basic_auth_rule_can_disable(gateway, db, key):
    g = gateway(acl=[{"http_basic_auth": "u:p", "disabled": ["GET"]}])
    db(0).set(f"{key}:g", "1")
    assert status(g, f"/GET/{key}:g") == 200
    assert status(g, f"/GET/{key}:g", [basic("u:p")]) == 403
    assert status(g, f"/GET/{key}:g", [basic("u:x")]) == 200


def test_later_matching_rules_take_precedence(gateway, db, key):
    g = gateway(acl=[{"enabled": ["SET"]}, {"disabled": ["SET"]}])
    assert status(g, f"/SET/{key}:a/1") == 403
    g = gateway(acl=[{"disabled": ["SET"]}, {"enabled": ["SET"]}])
    assert status(g, f"/SET/{key}:a/1") == 200
    g = gateway(acl=[
        {"disabled": ["SET", "DEL"]},
        {"http_basic_auth": "u:p", "enabled": ["SET", "DEL"]},
        {"ip": "127.0.0.0/8", "disabled": ["DEL"]},
    ], http_host="0.0.0.0")
    assert status(g, f"/SET/{key}:b/1", [basic("u:p")]) == 200
    assert status(g, f"/DEL/{key}:b", [basic("u:p")]) == 403
    assert db(0).get(f"{key}:b") == b"1"
    assert status(g, f"/DEL/{key}:b") == 403
    assert db(0).get(f"{key}:b") == b"1"


def test_disabled_wins_within_one_rule(gateway, key):
    g = gateway(acl=[{"enabled": ["SET"], "disabled": ["SET"]}])
    assert status(g, f"/SET/{key}:a/1") == 403
    g = gateway(acl=[{"enabled": ["SET"], "disabled": ["*"]}])
    assert status(g, f"/SET/{key}:a/1") == 403
    assert status(g, "/PING") == 403


def test_wildcard_with_exceptions(gateway, db, key):
    g = gateway(acl=[{"disabled": ["*"]}, {"enabled": ["GET", "ping"]}])
    db(0).set(f"{key}:a", "v")
    assert status(g, f"/SET/{key}:a/1") == 403
    assert status(g, f"/DEL/{key}:a") == 403
    assert db(0).get(f"{key}:a") == b"v"
    assert_json(get(g.port, f"/GET/{key}:a"), {"GET": "v"})
    assert_json(get(g.port, "/PING"), {"PING": [True, "PONG"]})
    assert status(g, "/ping") == 200


def test_ip_rules_match_client_address(gateway, db, valkey, key):
    other = own_address(valkey)
    assert not other.startswith("127."), other
    g = gateway(acl=[{"disabled": ["SET"]}, {"ip": "127.0.0.0/8", "enabled": ["SET"]}], http_host="0.0.0.0")
    assert status(g, f"/SET/{key}:lo/1") == 200
    assert status(g, f"/SET/{key}:ext/1", host=other) == 403
    assert db(0).get(f"{key}:lo") == b"1" and db(0).exists(f"{key}:ext") == 0
    # an address without a mask matches that host only
    g = gateway(acl=[{"disabled": ["SET"]}, {"ip": "127.0.0.1", "enabled": ["SET"]}], http_host="0.0.0.0")
    assert status(g, f"/SET/{key}:a/1") == 200
    assert status(g, f"/SET/{key}:a/1", host=other) == 403
    g = gateway(acl=[{"ip": other + "/32", "disabled": ["*"]}], http_host="0.0.0.0")
    assert status(g, f"/GET/{key}:a") == 200
    assert status(g, f"/GET/{key}:a", host=other) == 403
    net16 = ".".join(other.split(".")[:2]) + ".0.0/16"
    g = gateway(acl=[{"ip": net16, "disabled": ["GET"]}], http_host="0.0.0.0")
    assert status(g, f"/GET/{key}:a") == 200
    assert status(g, f"/GET/{key}:a", host=other) == 403


def test_rule_with_ip_and_basic_auth_needs_both(gateway, db, valkey, key):
    other = own_address(valkey)
    g = gateway(acl=[{"disabled": ["SET"]}, {"http_basic_auth": "u:p", "ip": "127.0.0.0/8", "enabled": ["SET"]}],
                http_host="0.0.0.0")
    assert status(g, f"/SET/{key}:a/1", [basic("u:p")]) == 200
    assert status(g, f"/SET/{key}:b/1", [basic("u:p")], host=other) == 403
    assert status(g, f"/SET/{key}:c/1") == 403
    assert db(0).get(f"{key}:a") == b"1" and db(0).exists(f"{key}:b", f"{key}:c") == 0


def test_transaction_and_select_commands_are_always_refused(gateway, db, key):
    g = gateway(acl=[{"enabled": ["*", "MULTI", "SELECT"]}])
    for path in ("/MULTI", "/EXEC", "/DISCARD", f"/WATCH/{key}:w", "/SELECT/3", "/select/3", "/multi"):
        assert status(g, path) == 403, path
    g = gateway()
    for path in ("/MULTI", "/EXEC", "/DISCARD", f"/WATCH/{key}:w", "/SELECT/3"):
        assert status(g, path) == 403, path
    assert_json(get(g.port, "/PING"), {"PING": [True, "PONG"]})
