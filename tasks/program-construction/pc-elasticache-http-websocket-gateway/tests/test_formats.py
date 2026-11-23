"""Reply-type mapping for every output format (adapted in part from upstream tests/basic.py)."""

from decimal import Decimal

import pytest

from gw import assert_json, assert_jsonp, get, loads, same, same_msgpack, unpack

WRONGTYPE = "WRONGTYPE Operation against a key holding the wrong kind of value"
NESTED = "/EVAL/return%20%7B1%2C%7B2%2C'x'%2C%7B%7D%7D%2C'y'%7D/0"  # return {1,{2,'x',{}},'y'}


@pytest.fixture
def seeded(db, key):
    r = db(0)
    r.set(f"{key}:s", "hello")
    r.set(f"{key}:n", "41")
    r.rpush(f"{key}:l", "a", "b", "c")
    r.hset(f"{key}:h", mapping={"f1": "v1", "f2": "v2"})
    r.zadd(f"{key}:z", {"m1": 1.5, "m2": 2})
    return key


@pytest.mark.timeout(600)
def test_json_scalar_reply_types(gateway, db, seeded):
    k = seeded
    g = gateway()
    assert_json(get(g.port, f"/TYPE/{k}:s"), {"TYPE": [True, "string"]})
    assert_json(get(g.port, "/PING"), {"PING": [True, "PONG"]})
    assert_json(get(g.port, f"/GET/{k}:s"), {"GET": "hello"})
    assert_json(get(g.port, f"/GET/{k}:none"), {"GET": None})
    assert_json(get(g.port, f"/INCR/{k}:n"), {"INCR": 42})
    assert db(0).get(f"{k}:n") == b"42"
    assert_json(get(g.port, f"/GET/{k}:l"), {"GET": [False, WRONGTYPE]})
    assert_json(get(g.port, f"/LPUSH/{k}:s/x"), {"LPUSH": [False, WRONGTYPE]})
    assert db(0).get(f"{k}:s") == b"hello"
    assert_json(get(g.port, "/EVAL/return%20redis.status_reply('FINE')/0"), {"EVAL": [True, "FINE"]})
    assert_json(get(g.port, "/EVAL/return%20redis.error_reply('BAD%20thing')/0"), {"EVAL": [False, "BAD thing"]})


def test_json_large_integers(gateway, db, key):
    g = gateway()
    db(0).set(f"{key}:big", "-9223372036854775808")
    assert_json(get(g.port, f"/INCRBY/{key}:big/0"), {"INCRBY": -9223372036854775808})
    db(0).set(f"{key}:b2", "4294967296")
    assert_json(get(g.port, f"/INCRBY/{key}:b2/1"), {"INCRBY": 4294967297})
    r = get(g.port, f"/INCRBY/{key}:big/-1")
    assert_json(r, {"INCRBY": [False, "ERR increment or decrement would overflow"]})
    assert db(0).get(f"{key}:big") == b"-9223372036854775808"


def test_json_arrays_and_special_commands(gateway, seeded):
    k = seeded
    g = gateway()
    assert_json(get(g.port, f"/LRANGE/{k}:l/0/-1"), {"LRANGE": ["a", "b", "c"]})
    assert_json(get(g.port, f"/LRANGE/{k}:none/0/-1"), {"LRANGE": []})
    assert_json(get(g.port, f"/MGET/{k}:s/{k}:none/{k}:n"), {"MGET": ["hello", None, "41"]})
    assert_json(get(g.port, f"/HGETALL/{k}:h"), {"HGETALL": {"f1": "v1", "f2": "v2"}})
    assert_json(get(g.port, f"/HGETALL/{k}:none"), {"HGETALL": {}})
    assert_json(get(g.port, f"/ZRANGE/{k}:z/0/-1/WITHSCORES"), {"ZRANGE": ["m1", "1.5", "m2", "2"]})
    assert_json(get(g.port, f"/HMGET/{k}:h/f2/nope/f1"), {"HMGET": ["v2", None, "v1"]})
    assert_json(get(g.port, NESTED), {"EVAL": [1, [2, "x", []], "y"]})


def test_json_info_is_parsed_into_an_object(gateway, db):
    g = gateway()
    r = get(g.port, "/INFO/server")
    assert r.status == 200 and r.header("Content-Type") == "application/json"
    obj = loads(r.body)
    assert list(obj) == ["INFO"] and isinstance(obj["INFO"], dict)
    info = obj["INFO"]
    assert all(type(v) is str for v in info.values())
    server = db(0).info("server")
    assert info["run_id"] == server["run_id"]
    assert info["process_id"] == str(server["process_id"])
    assert info["redis_mode"] == "standalone"
    assert info["valkey_version"] == str(server["valkey_version"])
    # the reply is an object, not the raw text
    assert "\r\n" not in "".join(info.values())


def test_json_stream_replies(gateway, db, key):
    g = gateway()
    r = db(0)
    x, y = f"{key}:x", f"{key}:y"
    r.xadd(x, {"f1": "v1", "f2": "v2"}, id="1-1")
    r.xadd(x, {"g": "w"}, id="2-1")
    r.xadd(y, {"a": "b"}, id="5-0")
    e1 = {"id": "1-1", "msg": {"f1": "v1", "f2": "v2"}}
    e2 = {"id": "2-1", "msg": {"g": "w"}}
    assert_json(get(g.port, f"/XRANGE/{x}/-/%2B"), {"XRANGE": [e1, e2]})
    assert_json(get(g.port, f"/XREVRANGE/{x}/%2B/-"), {"XREVRANGE": [e2, e1]})
    assert_json(get(g.port, f"/XRANGE/{x}/-/%2B/COUNT/1"), {"XRANGE": [e1]})
    assert_json(get(g.port, f"/XRANGE/{key}:none/-/%2B"), {"XRANGE": []})
    assert_json(get(g.port, f"/XREAD/STREAMS/{x}/0"), {"XREAD": {x: [e1, e2]}})
    assert_json(get(g.port, f"/XREAD/COUNT/1/STREAMS/{x}/{y}/0/0"),
                {"XREAD": {x: [e1], y: [{"id": "5-0", "msg": {"a": "b"}}]}})
    assert_json(get(g.port, f"/XREAD/STREAMS/{x}/9-0"), {"XREAD": None})
    r.xgroup_create(x, "grp", id="0")
    r.xgroup_create(x, "empty", id="$")
    assert_json(get(g.port, f"/XREADGROUP/GROUP/grp/c1/COUNT/1/STREAMS/{x}/%3E"), {"XREADGROUP": {x: [e1]}})
    assert_json(get(g.port, f"/XREADGROUP/GROUP/grp/c2/STREAMS/{x}/%3E"), {"XREADGROUP": {x: [e2]}})
    assert_json(get(g.port, f"/XREADGROUP/GROUP/empty/c/STREAMS/{x}/%3E"), {"XREADGROUP": None})
    pending = r.xpending(x, "grp")
    assert pending["pending"] == 2 and sorted(c["name"] for c in pending["consumers"]) == [b"c1", b"c2"]
    assert_json(get(g.port, f"/XPENDING/{x}/grp"),
                {"XPENDING": {"msgs": 2, "idmin": "1-1", "idmax": "2-1", "msgsperconsumer": {"c1": "1", "c2": "1"}}})
    assert_json(get(g.port, f"/XPENDING/{x}/empty"), {"XPENDING": {"msgs": 0}})

    def extended(path, expected):
        r_ = get(g.port, path)
        assert r_.status == 200 and r_.header("Content-Type") == "application/json"
        obj = loads(r_.body)
        assert list(obj) == ["XPENDING"] and isinstance(obj["XPENDING"], list) and len(obj["XPENDING"]) == len(expected), obj
        for item, (eid, owner, deliveries) in zip(obj["XPENDING"], expected):
            assert set(item) == {"id", "owner", "elapsedtime", "deliveries"}, item
            elapsed = item.pop("elapsedtime")
            assert isinstance(elapsed, Decimal) and elapsed >= 0, elapsed  # volatile: numeric only
            same(item, {"id": eid, "owner": owner, "deliveries": deliveries})

    extended(f"/XPENDING/{x}/grp/-/%2B/10", [("1-1", "c1", 1), ("2-1", "c2", 1)])
    extended(f"/XPENDING/{x}/grp/-/%2B/10/c1", [("1-1", "c1", 1)])
    assert_json(get(g.port, f"/XCLAIM/{x}/grp/c3/0/1-1"), {"XCLAIM": [e1]})
    assert_json(get(g.port, f"/XCLAIM/{x}/grp/c3/0/2-1/JUSTID"), {"XCLAIM": ["2-1"]})
    owners = {e["message_id"]: e["consumer"] for e in r.xpending_range(x, "grp", "-", "+", 10)}
    assert owners == {b"1-1": b"c3", b"2-1": b"c3"}
    extended(f"/XPENDING/{x}/grp/-/%2B/10", [("1-1", "c3", 2), ("2-1", "c3", 1)])
    # commands without a special shape keep the generic nested-array mapping
    assert_json(get(g.port, f"/XAUTOCLAIM/{x}/grp/c4/0/0-0"),
                {"XAUTOCLAIM": ["0-0", [["1-1", ["f1", "v1", "f2", "v2"]], ["2-1", ["g", "w"]]], []]})


def test_json_geo_replies(gateway, db, key):
    g = gateway()
    k = f"{key}:geo"
    db(0).geoadd(k, [13.361389, 38.115556, "Palermo", 15.087269, 37.502669, "Catania"])
    cat_xy = ["15.08726745843887329", "37.50266842333162032"]
    pal_xy = ["13.36138933897018433", "38.11555639549629859"]
    near = f"/GEORADIUS/{k}/15/37/200/km"
    assert_json(get(g.port, near + "/WITHCOORD/WITHDIST/WITHHASH/ASC"), {"GEORADIUS": [
        {"name": "Catania", "dist": "56.4413", "hash": 3479447370796909, "coords": cat_xy},
        {"name": "Palermo", "dist": "190.4424", "hash": 3479099956230698, "coords": pal_xy}]})
    assert_json(get(g.port, near + "/WITHDIST/ASC"),
                {"GEORADIUS": [{"name": "Catania", "dist": "56.4413"}, {"name": "Palermo", "dist": "190.4424"}]})
    assert_json(get(g.port, near + "/WITHCOORD/ASC"),
                {"GEORADIUS": [{"name": "Catania", "coords": cat_xy}, {"name": "Palermo", "coords": pal_xy}]})
    assert_json(get(g.port, near + "/WITHHASH/ASC"),
                {"GEORADIUS": [{"name": "Catania", "hash": 3479447370796909}, {"name": "Palermo", "hash": 3479099956230698}]})
    assert_json(get(g.port, near + "/ASC"), {"GEORADIUS": ["Catania", "Palermo"]})
    assert_json(get(g.port, f"/GEORADIUSBYMEMBER/{k}/Palermo/300/km/WITHDIST/ASC"),
                {"GEORADIUSBYMEMBER": [{"name": "Palermo", "dist": "0.0000"}, {"name": "Catania", "dist": "166.2742"}]})
    assert_json(get(g.port, f"/georadius/{k}/15/37/200/km/WITHDIST/ASC"),
                {"georadius": [{"name": "Catania", "dist": "56.4413"}, {"name": "Palermo", "dist": "190.4424"}]})
    # GEOSEARCH and GEOPOS are not special-cased
    assert_json(get(g.port, f"/GEOSEARCH/{k}/FROMLONLAT/15/37/BYRADIUS/200/km/WITHDIST/ASC"),
                {"GEOSEARCH": [["Catania", "56.4413"], ["Palermo", "190.4424"]]})
    assert_json(get(g.port, f"/GEOPOS/{k}/Palermo/nope"), {"GEOPOS": [pal_xy, None]})
    assert_json(get(g.port, f"/GEODIST/{k}/Palermo/Catania/km"), {"GEODIST": "166.2742"})


def test_raw_resp_for_each_reply_type(gateway, db, seeded):
    k = seeded
    g = gateway()
    cases = {
        f"/SET/{k}:t/world.raw": b"+OK\r\n",
        f"/GET/{k}:s.raw": b"$5\r\nhello\r\n",
        f"/GET/{k}:none.raw": b"$-1\r\n",
        f"/INCR/{k}:n.raw": b":42\r\n",
        f"/LRANGE/{k}:l/0/-1.raw": b"*3\r\n$1\r\na\r\n$1\r\nb\r\n$1\r\nc\r\n",
        f"/LRANGE/{k}:none/0/-1.raw": b"*0\r\n",
        f"/HGETALL/{k}:h.raw": b"*4\r\n$2\r\nf1\r\n$2\r\nv1\r\n$2\r\nf2\r\n$2\r\nv2\r\n",
        f"/GET/{k}:l.raw": b"-" + WRONGTYPE.encode() + b"\r\n",
        "/PING.raw": b"+PONG\r\n",
    }
    for path, expected in cases.items():
        r = get(g.port, path)
        assert r.status == 200 and r.header("Content-Type") == "binary/octet-stream", (path, r)
        assert r.body == expected, (path, r.body)
    assert db(0).get(f"{k}:t") == b"world"


def test_raw_integers_large_and_inside_arrays(gateway, db, key):
    g = gateway()
    db(0).set(f"{key}:big", "-9223372036854775808")
    assert get(g.port, f"/INCRBY/{key}:big/0.raw").body == b":-9223372036854775808\r\n"
    db(0).set(f"{key}:b2", "0")
    assert get(g.port, f"/INCRBY/{key}:b2/4294967296.raw").body == b":4294967296\r\n"
    # integer elements of an array reply are written as bulk strings
    r = get(g.port, f"/BITFIELD/{key}:bf/INCRBY/i64/0/4294967296.raw")
    assert r.body == b"*1\r\n$10\r\n4294967296\r\n"
    r = get(g.port, "/EVAL/return%20%7B7%2C-12%2C'z'%7D/0.raw")
    assert r.body == b"*3\r\n$1\r\n7\r\n$3\r\n-12\r\n$1\r\nz\r\n"


def test_msgpack_output(gateway, db, seeded):
    k = seeded
    g = gateway()
    cases = {
        f"/SET/{k}:m/world.msg": {"SET": [True, "OK"]},
        f"/GET/{k}:s.msg": {"GET": "hello"},
        f"/GET/{k}:none.msg": {"GET": None},
        f"/INCR/{k}:n.msg": {"INCR": 42},
        f"/LRANGE/{k}:l/0/-1.msg": {"LRANGE": ["a", "b", "c"]},
        f"/MGET/{k}:s/{k}:none.msg": {"MGET": ["hello", None]},
        f"/HGETALL/{k}:h.msg": {"HGETALL": {"f1": "v1", "f2": "v2"}},
        f"/GET/{k}:l.msg": {"GET": [False, WRONGTYPE]},
    }
    for path, expected in cases.items():
        r = get(g.port, path)
        assert r.status == 200 and r.header("Content-Type") == "application/x-msgpack", (path, r)
        same_msgpack(unpack(r.body), expected)
    assert db(0).get(f"{k}:m") == b"world"


def test_custom_type_extensions(gateway, db, key):
    g = gateway()
    db(0).set(f"{key}:v", "x<y>")
    types = {
        "txt": "text/plain", "html": "text/html", "xhtml": "application/xhtml+xml", "xml": "text/xml",
        "css": "text/css", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "bin": "binary/octet-stream",
    }
    for ext, content_type in types.items():
        r = get(g.port, f"/GET/{key}:v.{ext}")
        assert r.status == 200 and r.header("Content-Type") == content_type and r.body == b"x<y>", (ext, r)
    r = get(g.port, f"/GET/{key}:v.js")
    assert_json(r, {"GET": "x<y>"}, content_type="application/javascript")


def test_custom_type_reply_kinds(gateway, db, seeded):
    k = seeded
    g = gateway()

    def body(path, status=200):
        r = get(g.port, path)
        assert r.status == status, (path, r)
        return r.body

    assert body(f"/INCR/{k}:n.txt") == b"42"
    assert body(f"/TYPE/{k}:s.txt") == b"+string"
    assert body(f"/GET/{k}:l.txt") == b"-" + WRONGTYPE.encode()
    assert body(f"/LRANGE/{k}:l/0/-1.txt") == b"abc"
    assert body(f"/LRANGE/{k}:l/0/-1.txt?sep=--") == b"a--b--c"
    assert body(f"/MGET/{k}:s/{k}:none/{k}:n.txt?sep=|") == b"hello|42"
    assert body(f"/HGETALL/{k}:h.txt?sep=,") == b"f1,v1,f2,v2"
    assert body(f"/LRANGE/{k}:none/0/-1.txt") == b""
    assert body(f"/GET/{k}:none.txt", status=404) == b""
    assert body(f"/GET/{k}:none.html", status=404) == b""


def test_type_and_sep_query_parameters(gateway, db, seeded):
    k = seeded
    g = gateway()
    r = get(g.port, f"/GET/{k}:s?type=application/pdf")
    assert r.status == 200 and r.header("Content-Type") == "application/pdf" and r.body == b"hello"
    # ?type= overrides a JSON extension too
    r = get(g.port, f"/GET/{k}:s.json?type=a/b")
    assert r.header("Content-Type") == "a/b" and r.body == b"hello"
    r = get(g.port, f"/LRANGE/{k}:l/0/-1?type=text/csv&sep=;")
    assert r.header("Content-Type") == "text/csv" and r.body == b"a;b;c"
    # sep alone does not change JSON output
    assert_json(get(g.port, f"/LRANGE/{k}:l/0/-1?sep=,"), {"LRANGE": ["a", "b", "c"]})


def test_jsonp_wrapping(gateway, db, seeded):
    k = seeded
    g = gateway()
    r = get(g.port, f"/TYPE/{k}:s?jsonp=myFunction")
    assert r.status == 200 and r.header("Content-Type") == "application/json"
    assert_jsonp(r.body, "myFunction", {"TYPE": [True, "string"]})
    assert_jsonp(get(g.port, f"/GET/{k}:s?callback=cb").body, "cb", {"GET": "hello"})
    assert_jsonp(get(g.port, f"/LRANGE/{k}:l/0/-1?a=1&jsonp=z&b=2").body, "z", {"LRANGE": ["a", "b", "c"]})
    r = get(g.port, f"/GET/{k}:s.js?jsonp=cb")
    assert r.header("Content-Type") == "application/javascript"
    assert_jsonp(r.body, "cb", {"GET": "hello"})
    # non-JSON formats ignore it
    assert get(g.port, f"/GET/{k}:s.raw?jsonp=cb").body == b"$5\r\nhello\r\n"
    assert get(g.port, f"/GET/{k}:s.txt?jsonp=cb").body == b"hello"
    same_msgpack(unpack(get(g.port, f"/GET/{k}:s.msg?jsonp=cb").body), {"GET": "hello"})
