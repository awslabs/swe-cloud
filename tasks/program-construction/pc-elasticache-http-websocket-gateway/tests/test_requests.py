"""URL-to-command mapping, HTTP verbs and request-level features (adapted in part from upstream tests/basic.py)."""

import pytest

from gw import assert_json, assert_jsonp, etag_of, get, loads, request, same


@pytest.mark.timeout(600)
def test_set_then_get_round_trip_and_state(gateway, db, key):
    g = gateway()
    r = get(g.port, f"/SET/{key}:a/hello")
    assert_json(r, {"SET": [True, "OK"]})
    assert db(0).get(f"{key}:a") == b"hello"
    r = get(g.port, f"/GET/{key}:a")
    assert_json(r, {"GET": "hello"})
    r = get(g.port, f"/GET/{key}:missing")
    assert_json(r, {"GET": None})
    assert db(0).exists(f"{key}:missing") == 0


def test_percent_escapes_and_plus_decode_in_arguments(gateway, db, key):
    g = gateway()
    r = get(g.port, f"/SET/{key}%3Ax%2fy/a%2fb%2ec%20d+e%2Bf")
    assert_json(r, {"SET": [True, "OK"]})
    assert db(0).get(f"{key}:x/y") == b"a/b.c d e+f"
    # malformed or partial escapes are kept literally
    get(g.port, f"/SET/{key}:bad/a%zzb%4")
    assert db(0).get(f"{key}:bad") == b"a%zzb%4"
    get(g.port, f"/SET/{key}:bin/%00%0d%0a%c3%a9%41")
    assert db(0).get(f"{key}:bin") == b"\x00\r\n\xc3\xa9A"
    r = get(g.port, f"/GET/{key}%3abin.raw")
    assert r.body == b"$6\r\n\x00\r\n\xc3\xa9A\r\n"


def test_command_name_is_not_decoded_and_keeps_its_case(gateway, db, key):
    g = gateway()
    r = get(g.port, "/E%43HO/hi")
    obj = loads(r.body)
    assert list(obj) == ["E%43HO"]
    assert obj["E%43HO"][0] is False and "unknown command" in obj["E%43HO"][1]
    assert_json(get(g.port, "/echo/hi"), {"echo": "hi"})
    assert_json(get(g.port, "/EcHo/hi"), {"EcHo": "hi"})
    db(0).hset(f"{key}:h", "f", "v")
    assert_json(get(g.port, f"/hgetall/{key}:h"), {"hgetall": {"f": "v"}})


def test_extension_parsing_of_last_argument(gateway, db, key):
    g = gateway()
    cases = {
        "a.b.c": b"a.b.c",          # unknown extension stays in the argument
        "a.b.json": b"a.b",         # only the final known extension is stripped
        "x.raw.json": b"x.raw",
        "x.JSON": b"x.JSON",        # extensions are case-sensitive
        "a.": b"a.",
        "x.unknownext": b"x.unknownext",
        "%2ejson": b".json",        # an escaped dot is not an extension separator
    }
    for i, (arg, stored) in enumerate(cases.items()):
        r = get(g.port, f"/SET/{key}:e{i}/{arg}")
        assert r.status == 200, (arg, r)
        assert db(0).get(f"{key}:e{i}") == stored, arg
    assert_json(get(g.port, "/ECHO/.json"), {"ECHO": ""})
    # the extension of a key that is not the last argument is part of the key
    get(g.port, f"/SET/{key}:dot.txt/v")
    assert db(0).get(f"{key}:dot.txt") == b"v"


def test_empty_arguments_are_kept(gateway, db, key):
    g = gateway()
    assert_json(get(g.port, f"/SET/{key}:empty/"), {"SET": [True, "OK"]})
    assert db(0).get(f"{key}:empty") == b""
    assert_json(get(g.port, f"/GET/{key}:empty"), {"GET": ""})
    assert_json(get(g.port, "/ECHO/"), {"ECHO": ""})
    # "/ECHO//b" sends two arguments ("" and "b") and Redis rejects the arity
    obj = loads(get(g.port, "/ECHO//b").body)
    same(obj, {"ECHO": [False, "ERR wrong number of arguments for 'echo' command"]})
    obj = loads(get(g.port, "/ECHO").body)
    same(obj, {"ECHO": [False, "ERR wrong number of arguments for 'echo' command"]})


def test_database_prefix_selects_database(gateway, db, key):
    g = gateway()
    assert_json(get(g.port, f"/5/SET/{key}:d/five"), {"SET": [True, "OK"]})
    assert db(5).get(f"{key}:d") == b"five"
    assert db(0).exists(f"{key}:d") == 0
    assert_json(get(g.port, f"/GET/{key}:d"), {"GET": None})
    assert_json(get(g.port, f"/5/GET/{key}:d"), {"GET": "five"})
    assert_json(get(g.port, f"/05/GET/{key}:d"), {"GET": "five"})
    db(0).set(f"{key}:d", "zero")
    assert_json(get(g.port, f"/0/GET/{key}:d"), {"GET": "zero"})
    # the default connection is unaffected by an earlier prefixed request
    assert_json(get(g.port, f"/GET/{key}:d"), {"GET": "zero"})
    # a lone number is a command name, not a database
    obj = loads(get(g.port, "/7").body)
    assert list(obj) == ["7"] and obj["7"][0] is False


def test_put_body_becomes_last_argument(gateway, db, key):
    g = gateway()
    body = "bin/a.ry?\x00\r\n%2f+é.raw".encode()
    r = request(g.port, "PUT", f"/SET/{key}:up", body=body)
    assert_json(r, {"SET": [True, "OK"]})
    assert db(0).get(f"{key}:up") == body
    r = get(g.port, f"/GET/{key}:up.png")
    assert r.status == 200 and r.header("Content-Type") == "image/png" and r.body == body
    # arbitrary bytes are returned unchanged (seeded directly in the cache)
    blob = bytes(range(256)) * 4
    db(0).set(f"{key}:blob", blob)
    r = get(g.port, f"/GET/{key}:blob.bin")
    assert r.header("Content-Type") == "binary/octet-stream" and r.body == blob
    assert get(g.port, f"/GET/{key}:blob.raw").body == b"$1024\r\n" + blob + b"\r\n"
    r = request(g.port, "PUT", f"/RPUSH/{key}:pl/a/b", body=b"c c")
    assert_json(r, {"RPUSH": 3})
    assert db(0).lrange(f"{key}:pl", 0, -1) == [b"a", b"b", b"c c"]
    # the extension still selects the format and is not part of the key
    r = request(g.port, "PUT", f"/SET/{key}:fmt.raw", body=b"val")
    assert r.status == 200 and r.header("Content-Type") == "binary/octet-stream" and r.body == b"+OK\r\n"
    assert db(0).get(f"{key}:fmt") == b"val" and db(0).exists(f"{key}:fmt.raw") == 0
    # database prefix with PUT
    request(g.port, "PUT", f"/4/SET/{key}:p4", body=b"four")
    assert db(4).get(f"{key}:p4") == b"four" and db(0).exists(f"{key}:p4") == 0


def test_put_with_empty_body_runs_path_command(gateway, db, key):
    g = gateway()
    r = request(g.port, "PUT", f"/SET/{key}:e/zz", body=b"")
    assert_json(r, {"SET": [True, "OK"]})
    assert db(0).get(f"{key}:e") == b"zz"


def test_post_body_is_the_command(gateway, db, key):
    g = gateway()
    r = request(g.port, "POST", "/", body=f"INCRBY/{key}:n/41".encode())
    assert_json(r, {"INCRBY": 41})
    assert db(0).get(f"{key}:n") == b"41"
    r = request(g.port, "POST", "/", body=f"GET/{key}:n.raw".encode())
    assert r.header("Content-Type") == "binary/octet-stream" and r.body == b"$2\r\n41\r\n"
    r = request(g.port, "POST", "/", body=f"SET/{key}:p/a%2fb+c".encode())
    assert_json(r, {"SET": [True, "OK"]})
    assert db(0).get(f"{key}:p") == b"a/b c"
    # the request path is ignored for POST, but its query string still applies
    r = request(g.port, "POST", f"/SET/{key}:ignored/yy?jsonp=cb", body=f"GET/{key}:n".encode())
    assert r.status == 200
    assert_jsonp(r.body, "cb", {"GET": "41"})
    assert db(0).exists(f"{key}:ignored") == 0
    r = request(g.port, "POST", "/", body=f"5/SET/{key}:p5/v".encode())
    assert_json(r, {"SET": [True, "OK"]})
    assert db(5).get(f"{key}:p5") == b"v"


def test_post_without_body_is_forbidden(gateway, db, key):
    g = gateway()
    r = request(g.port, "POST", "/", body=b"")
    assert r.status == 403 and r.body == b""


def test_root_and_default_root(gateway, db, key):
    g = gateway()
    r = get(g.port, "/")
    assert r.status == 403 and r.body == b""
    db(0).set(f"{key}:index", "<h1>hi</h1>")
    g2 = gateway(default_root=f"/GET/{key}:index.html")
    r = get(g2.port, "/")
    assert r.status == 200 and r.header("Content-Type") == "text/html" and r.body == b"<h1>hi</h1>"
    # other paths are unaffected
    assert_json(get(g2.port, f"/GET/{key}:index"), {"GET": "<h1>hi</h1>"})


def test_http_verbs(gateway, db, key):
    g = gateway()
    db(0).set(f"{key}:v", "1")
    for verb in ("DELETE", "HEAD"):
        r = request(g.port, verb, f"/DEL/{key}:v")
        assert r.status == 405, (verb, r)
        assert db(0).get(f"{key}:v") == b"1"
    r = request(g.port, "OPTIONS", f"/DEL/{key}:v")
    assert r.status == 200 and r.body == b""
    assert r.header("Content-Type") == "text/html"
    assert r.header("Allow") == "GET,POST,PUT,OPTIONS"
    assert r.header("Access-Control-Allow-Methods") == "GET,POST,PUT,OPTIONS"
    assert r.header("Access-Control-Allow-Origin") == "*"
    assert r.header("Access-Control-Allow-Headers") == "X-Requested-With, Content-Type, Authorization"
    assert db(0).get(f"{key}:v") == b"1"


def test_cors_headers_on_command_responses(gateway, key):
    g = gateway()
    r = get(g.port, f"/GET/{key}:x")
    assert r.header("Access-Control-Allow-Origin") == "*"
    assert r.header("Allow") == "GET,POST,PUT,OPTIONS"
    assert r.header("Access-Control-Allow-Methods") == "GET,POST,PUT,OPTIONS"


def test_crossdomain_policy(gateway):
    g = gateway()
    r = get(g.port, "/crossdomain.xml")
    assert r.status == 200 and r.header("Content-Type") == "application/xml"
    assert r.body == (
        b'<?xml version="1.0"?>\n'
        b'<!DOCTYPE cross-domain-policy SYSTEM "http://www.macromedia.com/xml/dtds/cross-domain-policy.dtd">\n'
        b"<cross-domain-policy>\n"
        b'<allow-access-from domain="*" />\n'
        b"</cross-domain-policy>\n"
    )


def test_etag_and_if_none_match(gateway, db, key):
    g = gateway()
    db(0).set(f"{key}:e", "world")
    for path in (f"/GET/{key}:e", f"/GET/{key}:e.raw", f"/GET/{key}:e.txt", f"/GET/{key}:e?jsonp=f"):
        r = get(g.port, path)
        assert r.status == 200
        tag = r.header("ETag")
        assert tag == etag_of(r.body), (path, r)
        r2 = get(g.port, path, headers=[("If-None-Match", tag)])
        assert r2.status == 304 and r2.body == b"", (path, r2)
        r3 = get(g.port, path, headers=[("If-None-Match", etag_of(b"nonsense"))])
        assert r3.status == 200 and r3.body == r.body
        r4 = get(g.port, path, headers=[("If-None-Match", tag.strip('"'))])
        assert r4.status == 200 and r4.body == r.body
    # the tag follows the stored value
    tag = get(g.port, f"/GET/{key}:e.txt").header("ETag")
    db(0).set(f"{key}:e", "changed")
    r = get(g.port, f"/GET/{key}:e.txt", headers=[("If-None-Match", tag)])
    assert r.status == 200 and r.body == b"changed"


def test_filename_query_sets_content_disposition(gateway, db, key):
    g = gateway()
    db(0).set(f"{key}:f", "data")
    r = get(g.port, f"/GET/{key}:f.txt?filename=report.txt")
    assert r.status == 200 and r.body == b"data"
    assert r.header("Content-Disposition") == 'attachment; filename="report.txt"'
    assert r.header("Content-Type") == "text/plain"
    r = get(g.port, f"/GET/{key}:f")
    assert r.header("Content-Disposition") is None
