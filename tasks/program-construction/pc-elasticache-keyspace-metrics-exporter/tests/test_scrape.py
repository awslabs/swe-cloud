"""The multi-target /scrape endpoint and its per-request option overrides."""

import urllib.parse

from harness import write_fixture
from metrics import assert_samples, select, stream, value_of

UNREACHABLE = "redis://127.0.0.1:1"


def query(**params):
    return "/scrape?" + urllib.parse.urlencode({k.replace("_", "-"): v for k, v in params.items()})


def ours(samples, prefix, filename=None):
    def keep(s):
        if s[0] == "script_result" or s[0] == "script_values":
            return s[1].get("filename") == filename
        if s[0] in ("key_group_count", "number_of_distinct_key_groups"):
            return s[1].get("db") == "db15"
        return any(prefix in v for v in s[1].values())
    return select(samples, keep)


def size(db, key, n):
    return ("key_size", {"db": db, "key": key}, n)


def test_scrape_collects_from_the_target(service, keyspace):
    p, engine = keyspace.prefix, keyspace.engine
    keyspace.db(0).set(f"{p}:k", "8")
    keyspace.db(0).set(f"{p}:j", "x")
    running = service("-redis.addr", UNREACHABLE, "-check-single-keys", f"{p}:k", "-count-keys", f"{p}:*")

    samples = running.samples()
    assert value_of(samples, "up") == 0
    assert ours(samples, p) == []

    expected = [size("db0", f"{p}:k", 1), ("key_value", {"db": "db0", "key": f"{p}:k"}, 8),
                ("keys_count", {"db": "db0", "key": f"{p}:*"}, 2)]
    for target in (engine.url, f"{engine.host}:{engine.port}"):
        samples = running.samples(query(target=target))
        assert value_of(samples, "up") == 1
        assert_samples(ours(samples, p), expected)


def test_query_parameters_override_flags_for_one_request(service, keyspace, scratch):
    p = keyspace.prefix
    r0, r1, r15 = keyspace.db(0), keyspace.db(1), keyspace.reserve(15)
    r0.set(f"{p}:a1", "1")
    r0.set(f"{p}:a2", "x")
    r0.set(f"{p}:b", "2")
    r0.set(f"{p}:c", "3")
    r0.rpush(f"{p}:list", "a", "b")
    r0.sadd(f"{p}:set", "a")
    r0.xadd(f"{p}:st0", {"a": "1"}, id="7-0")
    r1.xadd(f"{p}:st", {"a": "1"}, id="10-0")
    r1.xadd(f"{p}:st", {"a": "1"}, id="20-0")
    for key in ("g0:0", "g1:1", "g0:2"):
        r15.set(f"{p}:{key}", "v")
    write_fixture(scratch / "s.lua", f'return {{"{p}_v", "1"}}', mode=0o644)
    running = service("-redis.addr", keyspace.engine.url, "-check-keys", f"{p}:a*", "-check-single-keys", f"{p}:b",
                      "-count-keys", f"{p}:*", "-check-key-groups", f"^({p}:g%d):", "-script", "s.lua",
                      "-check-single-streams", f"db1={p}:st", cwd=scratch)

    a_keys = [size("db0", f"{p}:a1", 1), ("key_value", {"db": "db0", "key": f"{p}:a1"}, 1),
              size("db0", f"{p}:a2", 1), ("key_value_as_string", {"db": "db0", "key": f"{p}:a2", "val": "x"}, 1)]
    b_key = [size("db0", f"{p}:b", 1), ("key_value", {"db": "db0", "key": f"{p}:b"}, 2)]
    count_all = [("keys_count", {"db": "db0", "key": f"{p}:*"}, 7)]
    st = stream("db1", f"{p}:st", length=2, keys=1, nodes=2, last_generated=20, groups=0,
                max_deleted=0, first=10, last=20, added=2)
    st0 = stream("db0", f"{p}:st0", length=1, keys=1, nodes=2, last_generated=7, groups=0,
                 max_deleted=0, first=7, last=7, added=1)
    always = [
        ("key_group_count", {"db": "db15", "key_group": f"{p}:g0"}, 2),
        ("key_group_count", {"db": "db15", "key_group": f"{p}:g1"}, 1),
        ("number_of_distinct_key_groups", {"db": "db15"}, 2),
        ("script_result", {"filename": "s.lua"}, 1),
        ("script_values", {"filename": "s.lua", "key": f"{p}_v"}, 1),
    ]
    target = keyspace.engine.url
    flags_only = a_keys + b_key + count_all + st + always

    assert_samples(ours(running.samples(query(target=target)), p, "s.lua"), flags_only)

    got = ours(running.samples(query(target=target, check_single_keys=f"{p}:c")), p, "s.lua")
    assert_samples(got, a_keys + [size("db0", f"{p}:c", 1), ("key_value", {"db": "db0", "key": f"{p}:c"}, 3)]
                   + count_all + st + always)

    got = ours(running.samples(query(target=target, check_keys=f"{p}:list,{p}:set", count_keys=f"db1={p}:*",
                                     check_single_streams=f"{p}:st0")), p, "s.lua")
    assert_samples(got, b_key + [size("db0", f"{p}:list", 2), size("db0", f"{p}:set", 1),
                                 ("keys_count", {"db": "db1", "key": f"{p}:*"}, 1)] + st0 + always)

    got = ours(running.samples(query(target=target, check_streams=f"{p}:st0*", check_single_keys="")), p, "s.lua")
    assert_samples(got, a_keys + b_key + count_all + st + st0 + always)

    assert_samples(ours(running.samples(), p, "s.lua"), flags_only)


def test_scrape_request_errors(service, keyspace):
    p, url = keyspace.prefix, keyspace.engine.url
    keyspace.db(0).set(f"{p}:k", "1")
    running = service("-redis.addr", url, "-check-single-keys", f"{p}:k")

    assert running.get("/scrape")[0] == 400
    assert running.get(query(target=url, check_keys=f"dbz={p}:*"))[0] == 400
    assert running.get(query(target=url, count_keys=f"{p}:%zz"))[0] == 400
    assert running.get(query(target=url, check_single_streams=f"db0={p}:a=b"))[0] == 400

    samples = running.samples(query(target=UNREACHABLE))
    assert value_of(samples, "up") == 0
    assert ours(samples, p) == []

    samples = running.samples()
    assert value_of(samples, "up") == 1
    assert_samples(ours(samples, p), [size("db0", f"{p}:k", 1), ("key_value", {"db": "db0", "key": f"{p}:k"}, 1)])
