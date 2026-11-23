"""Per-key size/value gauges (-check-keys, -check-single-keys) and key counts (-count-keys)."""

import math

import pytest

from metrics import assert_samples, mentions, select, value_of

NAN = math.nan
INF = math.inf


def collect(service, engine, prefix, *args):
    running = service("-redis.addr", engine.url, *args)
    samples = running.samples()
    assert value_of(samples, "up") == 1
    return select(samples, mentions(prefix))


def size(db, key, value):
    return ("key_size", {"db": db, "key": key}, value)


def number(db, key, value):
    return ("key_value", {"db": db, "key": key}, value)


def text(db, key, val):
    return ("key_value_as_string", {"db": db, "key": key, "val": val}, 1)


def test_size_depends_on_key_type(service, keyspace):
    p, r = keyspace.prefix, keyspace.db(0)
    r.set(f"{p}:t:num", "42.5")
    r.set(f"{p}:t:str", "hello")
    r.rpush(f"{p}:t:list", "a", "b", "c")
    r.sadd(f"{p}:t:set", "x", "y")
    r.zadd(f"{p}:t:zset", {"m1": 1, "m2": 2, "m3": 3, "m4": 4})
    r.hset(f"{p}:t:hash", mapping={f"f{i}": i for i in range(5)})
    r.pfadd(f"{p}:t:hll", "u1", "u2", "u3")
    for i in range(3):
        r.xadd(f"{p}:t:stream", {"n": str(i)}, id=f"{1700000000000 + i}-0")
    r.set(f"{p}:other", "7")  # not matched by the pattern

    got = collect(service, keyspace.engine, p, "-check-keys", f"db0={p}:t:*")
    assert_samples(got, [
        size("db0", f"{p}:t:num", 4), number("db0", f"{p}:t:num", 42.5),
        size("db0", f"{p}:t:str", 5), text("db0", f"{p}:t:str", "hello"),
        size("db0", f"{p}:t:list", 3),
        size("db0", f"{p}:t:set", 2),
        size("db0", f"{p}:t:zset", 4),
        size("db0", f"{p}:t:hash", 5),
        size("db0", f"{p}:t:hll", 3),
        size("db0", f"{p}:t:stream", 3),
    ])


# (stored value, STRLEN, expected export) observed natively; None means no value gauge.
COMMON_VALUES = {
    "int": ("42", 2, 42),
    "neg": ("-0.25", 5, -0.25),
    "dot": (".5", 2, 0.5),
    "exp": ("1e3", 3, 1000),
    "plus": ("+5", 2, 5),
    "inf": ("inf", 3, INF),
    "ninf": ("-Inf", 4, -INF),
    "nan": ("NaN", 3, NAN),
    "word": ("true", 4, "true"),
    "lead": (" 1", 2, " 1"),
    "trail": ("1 ", 2, "1 "),
    "quote": ('a"b\\c', 5, 'a"b\\c'),
    "newline": ("x\ny", 3, "x\ny"),
    "huge": ("1e400", 5, "1e400"),
    "empty": ("", 0, None),
}

EXTENDED_VALUES = {
    "hexfloat": ("0x1p4", 5, 16),
    "hexint": ("0x10", 4, "0x10"),
    "underscore": ("1_000", 5, 1000),
    "infinity": ("Infinity", 8, INF),
}


def expected_values(prefix, table):
    out = []
    for name, (_, length, export) in table.items():
        key = f"{prefix}:v:{name}"
        out.append(size("db0", key, length))
        if isinstance(export, str):
            out.append(text("db0", key, export))
        elif export is not None:
            out.append(number("db0", key, export))
    return out


@pytest.mark.parametrize("table", [COMMON_VALUES, EXTENDED_VALUES], ids=["common", "extended"])
def test_string_values_as_number_or_label(service, keyspace, table):
    p, r = keyspace.prefix, keyspace.db(0)
    for name, (stored, _, _) in table.items():
        r.set(f"{p}:v:{name}", stored)
    got = collect(service, keyspace.engine, p, "-check-keys", f"{p}:v:*")
    assert_samples(got, expected_values(p, table))


def test_disable_exporting_key_values(service, keyspace):
    p, r = keyspace.prefix, keyspace.db(0)
    r.set(f"{p}:num", "42")
    r.set(f"{p}:word", "hello")
    r.pfadd(f"{p}:hll", "a", "b")
    got = collect(service, keyspace.engine, p, "-check-keys", f"{p}:*", "-disable-exporting-key-values")
    assert_samples(got, [size("db0", f"{p}:num", 2), size("db0", f"{p}:word", 5), size("db0", f"{p}:hll", 2)])


def test_single_keys_are_exact_names(service, keyspace):
    p, r = keyspace.prefix, keyspace.db(0)
    r.set(f"{p}:*lit", "star")
    r.set(f"{p}:alit", "1")
    r.set(f"{p}:blit", "2")
    got = collect(service, keyspace.engine, p,
                  "-check-single-keys", f"{p}:*lit,{p}:missing",
                  "-check-keys", f"{p}:zz*")
    assert_samples(got, [
        size("db0", f"{p}:*lit", 4), text("db0", f"{p}:*lit", "star"),
        size("db0", f"{p}:missing", 0),
    ])


def test_check_keys_without_glob_is_a_direct_lookup(service, keyspace):
    p, r = keyspace.prefix, keyspace.db(0)
    r.set(f"{p}:plain", "9")
    r.set(f"{p}:plainer", "10")
    got = collect(service, keyspace.engine, p, "-check-keys", f"{p}:plain,{p}:absent")
    assert_samples(got, [
        size("db0", f"{p}:plain", 1), number("db0", f"{p}:plain", 9),
        size("db0", f"{p}:absent", 0),
    ])


def test_other_glob_characters_also_scan(service, keyspace):
    p, r = keyspace.prefix, keyspace.db(0)
    r.set(f"{p}:kay", "1")
    r.set(f"{p}:kby", "2")
    r.set(f"{p}:kcy", "3")
    r.set(f"{p}:kaay", "4")
    got = collect(service, keyspace.engine, p,
                  "-check-keys", f"{p}:k?y,{p}:x^y",
                  "-count-keys", f"{p}:k[ab]y,{p}:k?y")
    count = lambda pattern, n: ("keys_count", {"db": "db0", "key": pattern}, n)  # noqa: E731
    assert_samples(got, [
        size("db0", f"{p}:kay", 1), number("db0", f"{p}:kay", 1),
        size("db0", f"{p}:kby", 1), number("db0", f"{p}:kby", 2),
        size("db0", f"{p}:kcy", 1), number("db0", f"{p}:kcy", 3),
        count(f"{p}:k[ab]y", 2), count(f"{p}:k?y", 3),
    ])


def test_key_list_items_are_url_unescaped(service, keyspace):
    p, r = keyspace.prefix, keyspace.db(0)
    for name, value in {"a b": "sp", "a+b": "plus", "c,d": "comma", "x=y": "eq", "pct%": "pct"}.items():
        r.set(f"{p}:{name}", value)
    got = collect(service, keyspace.engine, p,
                  "-check-single-keys", f"{p}:a+b,{p}:a%2Bb,{p}:c%2Cd,{p}:x%3Dy,{p}:pct%25")
    assert_samples(got, [
        size("db0", f"{p}:a b", 2), text("db0", f"{p}:a b", "sp"),
        size("db0", f"{p}:a+b", 4), text("db0", f"{p}:a+b", "plus"),
        size("db0", f"{p}:c,d", 5), text("db0", f"{p}:c,d", "comma"),
        size("db0", f"{p}:x=y", 2), text("db0", f"{p}:x=y", "eq"),
        size("db0", f"{p}:pct%", 3), text("db0", f"{p}:pct%", "pct"),
    ])


def test_database_prefixes_select_databases(service, keyspace):
    p = keyspace.prefix
    keyspace.db(0).set(f"{p}:k", "1")
    keyspace.db(1).set(f"{p}:k", "2")
    keyspace.db(3).rpush(f"{p}:l3", "a")
    keyspace.db(3).set(f"{p}:three", "3")
    got = collect(service, keyspace.engine, p,
                  "-check-single-keys", f"{p}:k,db1={p}:k, db3 = {p}:l3 ,3={p}:three,,db0=,=x")
    assert_samples(got, [
        size("db0", f"{p}:k", 1), number("db0", f"{p}:k", 1),
        size("db1", f"{p}:k", 1), number("db1", f"{p}:k", 2),
        size("db3", f"{p}:l3", 1),
        size("db3", f"{p}:three", 1), number("db3", f"{p}:three", 3),
    ])


def test_count_keys_per_pattern(service, keyspace):
    p = keyspace.prefix
    for i in range(4):
        keyspace.db(0).set(f"{p}:c:{i}", "x")
    keyspace.db(0).rpush(f"{p}:c:list", "x")
    for i in range(2):
        keyspace.db(1).set(f"{p}:c:{i}", "x")
    keyspace.db(0).set(f"{p}:d:0", "x")
    got = collect(service, keyspace.engine, p,
                  "-count-keys", f"{p}:c:*,db1={p}:c:*,db2={p}:c:*,{p}:none*,{p}:c:1",
                  "-check-keys-batch-size", "2")
    count = lambda db, pattern, n: ("keys_count", {"db": db, "key": pattern}, n)  # noqa: E731
    assert_samples(got, [
        count("db0", f"{p}:c:*", 5),
        count("db1", f"{p}:c:*", 2),
        count("db2", f"{p}:c:*", 0),
        count("db0", f"{p}:none*", 0),
        count("db0", f"{p}:c:1", 1),
    ])


def test_scan_reports_every_match_in_small_batches(service, keyspace):
    p, r = keyspace.prefix, keyspace.db(0)
    for i in range(10):
        r.set(f"{p}:n:{i}", str(i))
    got = collect(service, keyspace.engine, p, "-check-keys", f"{p}:n:*", "-check-keys-batch-size", "2")
    expected = []
    for i in range(10):
        expected += [size("db0", f"{p}:n:{i}", 1), number("db0", f"{p}:n:{i}", i)]
    assert_samples(got, expected)


def test_each_request_collects_current_data(service, keyspace):
    p, r = keyspace.prefix, keyspace.db(0)
    r.set(f"{p}:w:a", "1")
    running = service("-redis.addr", keyspace.engine.url, "-check-keys", f"{p}:w:*", "-count-keys", f"{p}:w:*")
    count = lambda n: ("keys_count", {"db": "db0", "key": f"{p}:w:*"}, n)  # noqa: E731

    assert_samples(select(running.samples(), mentions(p)),
                   [size("db0", f"{p}:w:a", 1), number("db0", f"{p}:w:a", 1), count(1)])

    r.set(f"{p}:w:a", "two")
    r.rpush(f"{p}:w:b", "x", "y")
    assert_samples(select(running.samples(), mentions(p)), [
        size("db0", f"{p}:w:a", 3), text("db0", f"{p}:w:a", "two"),
        size("db0", f"{p}:w:b", 2), count(2),
    ])

    r.delete(f"{p}:w:a")
    assert_samples(select(running.samples(), mentions(p)), [size("db0", f"{p}:w:b", 2), count(1)])
