"""Stream, consumer-group and consumer gauges (-check-streams, -check-single-streams)."""

from metrics import assert_samples, consumer, group, mentions, select, stream, value_of


def collect(service, keyspace, *args):
    running = service("-redis.addr", keyspace.engine.url, *args)
    samples = running.samples()
    assert value_of(samples, "up") == 1
    return select(samples, mentions(keyspace.prefix))


def seed_busy_stream(r, key):
    for i in range(5):
        r.xadd(key, {"n": str(i)}, id=f"{1700000000000 + i}-{i}")
    r.xgroup_create(key, "g1", id="0")
    r.xgroup_create(key, "g2", id="$")
    r.xreadgroup("g1", "c1", {key: ">"}, count=2)
    r.xreadgroup("g1", "c2", {key: ">"}, count=1)
    r.xack(key, "g1", "1700000000000-0")
    r.execute_command("XGROUP", "CREATECONSUMER", key, "g2", "idle")


def busy_stream_expectation(key, with_consumers=True):
    rows = stream("db0", key, length=5, keys=1, nodes=2, last_generated=1700000000004, groups=2,
                  max_deleted=0, first=1700000000000, last=1700000000004, added=5)
    rows += group("db0", key, "g1", consumers=2, pending=2, last_delivered=1700000000002, entries_read=3, lag=2)
    rows += group("db0", key, "g2", consumers=1, pending=0, last_delivered=1700000000004, entries_read=0, lag=0)
    if with_consumers:
        rows += consumer("db0", key, "g1", "c1", 1) + consumer("db0", key, "g1", "c2", 1)
        rows += consumer("db0", key, "g2", "idle", 0)
    return rows


def test_stream_groups_and_consumers(service, keyspace):
    key = f"{keyspace.prefix}:busy"
    seed_busy_stream(keyspace.db(0), key)
    got = collect(service, keyspace, "-check-single-streams", key)
    assert_samples(got, busy_stream_expectation(key))


def test_exclude_consumer_metrics(service, keyspace):
    key = f"{keyspace.prefix}:busy"
    seed_busy_stream(keyspace.db(0), key)
    got = collect(service, keyspace, "-check-streams", f"{keyspace.prefix}:b*", "-streams-exclude-consumer-metrics")
    assert_samples(got, busy_stream_expectation(key, with_consumers=False))
    assert [s for s in got if "consumer" in s[1]] == []


def test_deleted_entries_and_empty_streams(service, keyspace):
    p, r = keyspace.prefix, keyspace.db(0)
    trimmed = f"{p}:s2"
    for i in range(4):
        r.xadd(trimmed, {"n": str(i)}, id=f"{1700000001000 + i}-0")
    r.xgroup_create(trimmed, "g", id="0")
    r.xreadgroup("g", "c", {trimmed: ">"}, count=1)
    r.xdel(trimmed, "1700000001002-0")
    empty = f"{p}:s3"
    r.xgroup_create(empty, "empty", id="$", mkstream=True)
    drained = f"{p}:s4"
    r.xadd(drained, {"a": "1"}, id="5-1")
    r.xdel(drained, "5-1")

    got = collect(service, keyspace, "-check-streams", f"{p}:s*")
    expected = stream("db0", trimmed, length=3, keys=1, nodes=2, last_generated=1700000001003, groups=1,
                      max_deleted=1700000001002, first=1700000001000, last=1700000001003, added=4)
    expected += group("db0", trimmed, "g", consumers=1, pending=1, last_delivered=1700000001000, entries_read=1, lag=0)
    expected += consumer("db0", trimmed, "g", "c", 1)
    expected += stream("db0", empty, length=0, keys=0, nodes=1, last_generated=0, groups=1,
                       max_deleted=0, first=0, last=0, added=0)
    expected += group("db0", empty, "empty", consumers=0, pending=0, last_delivered=0, entries_read=0, lag=0)
    expected += stream("db0", drained, length=0, keys=0, nodes=1, last_generated=5, groups=0,
                       max_deleted=5, first=0, last=0, added=1)
    assert_samples(got, expected)


def test_unknown_group_counters_report_zero(service, keyspace):
    key = f"{keyspace.prefix}:sg"
    r = keyspace.db(0)
    for i in range(5):
        r.xadd(key, {"n": str(i)}, id=f"{100 + i}-0")
    r.xgroup_create(key, "tail", id="$")
    r.xgroup_create(key, "mid", id="102-0")
    got = collect(service, keyspace, "-check-single-streams", key)
    expected = stream("db0", key, length=5, keys=1, nodes=2, last_generated=104, groups=2,
                      max_deleted=0, first=100, last=104, added=5)
    expected += group("db0", key, "tail", consumers=0, pending=0, last_delivered=104, entries_read=0, lag=0)
    expected += group("db0", key, "mid", consumers=0, pending=0, last_delivered=102, entries_read=0, lag=0)
    assert_samples(got, expected)


def test_only_existing_stream_keys_are_reported(service, keyspace):
    p = keyspace.prefix
    r0, r1, r3 = keyspace.db(0), keyspace.db(1), keyspace.db(3)
    r0.set(f"{p}:snot", "x")
    r0.rpush(f"{p}:slist", "x")
    r1.xadd(f"{p}:st", {"a": "1"}, id="10-0")
    r1.xadd(f"{p}:st", {"a": "1"}, id="20-0")
    r3.set(f"{p}:three", "3")
    r0.xadd(f"{p}:other", {"a": "1"}, id="7-0")  # in db0, not selected
    got = collect(service, keyspace,
                  "-check-streams", f"{p}:s*,db1={p}:s*",
                  "-check-single-streams", f"{p}:snot,{p}:smissing,db3={p}:three")
    assert_samples(got, stream("db1", f"{p}:st", length=2, keys=1, nodes=2, last_generated=20, groups=0,
                               max_deleted=0, first=10, last=20, added=2))
