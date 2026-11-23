"""Key-group classification (-check-key-groups) and overflow (-max-distinct-key-groups).

Key groups scan every database of the shared engine, so each test owns one reserved
database and asserts only on series of that database.
"""

import pytest

from metrics import GROUP_MEMORY, assert_samples, mentions, select, value_of


def group_samples(samples, db):
    label = f"db{db}"
    return select(samples, lambda s: s[1].get("db") == label, {"key_group_count", "number_of_distinct_key_groups"})


def expected(db, counts, distinct):
    label = f"db{db}"
    rows = [("key_group_count", {"db": label, "key_group": name}, n) for name, n in counts.items()]
    return rows + [("number_of_distinct_key_groups", {"db": label}, distinct)]


def memory_by_group(samples, db):
    """Every reported group in the database has a positive memory gauge and nothing else does."""
    label = f"db{db}"
    counts = {s[1]["key_group"] for s in samples if s[0] == "key_group_count" and s[1].get("db") == label}
    memory = [s for s in samples if s[0] == GROUP_MEMORY and s[1].get("db") == label]
    assert sorted(s[1]["key_group"] for s in memory) == sorted(counts)
    assert all(set(s[1]) == {"db", "key_group"} and s[2] > 0 for s in memory), memory
    return {s[1]["key_group"]: s[2] for s in memory}


def collect(service, keyspace, *args):
    running = service("-redis.addr", keyspace.engine.url, *args)
    samples = running.samples()
    assert value_of(samples, "up") == 1
    return samples


def test_first_matching_pattern_names_the_group(service, keyspace):
    p, r = keyspace.prefix, keyspace.reserve(8)
    for key in [f"{p}:alpha:1", f"{p}:alpha:2", f"{p}:alpha:x", f"{p}:beta:1", f"{p}:gamma.x", f"{p}:9", f"U{p}"]:
        r.set(key, "v")
    samples = collect(service, keyspace, "-check-key-groups", f" ^({p}):(%a+):%d$ ,, ^({p}):(%a+)")
    assert_samples(group_samples(samples, 8), expected(8, {
        f"{p}alpha": 3, f"{p}beta": 1, f"{p}gamma": 1, "unclassified": 2,
    }, 4))
    memory_by_group(samples, 8)
    distinct_dbs = sorted(s[1]["db"] for s in samples if s[0] == "number_of_distinct_key_groups")
    assert distinct_dbs == sorted(f"db{n}" for n in range(16))  # the engine has 16 databases


def test_pattern_without_captures_yields_empty_group_name(service, keyspace):
    p, r = keyspace.prefix, keyspace.reserve(9)
    r.set(f"{p}:pos", "v")
    r.set(f"{p}:other:1", "v")
    samples = collect(service, keyspace, "-check-key-groups", f"^{p}:pos$,^({p}):(%a+)")
    assert_samples(group_samples(samples, 9), expected(9, {"": 1, f"{p}other": 1}, 2))
    memory_by_group(samples, 9)


def test_group_list_is_parsed_as_csv(service, keyspace):
    p, r = keyspace.prefix, keyspace.reserve(10)
    for key in ["alpha:1", "alpha:2", "beta:1", "gamma.x", "pos"]:
        r.set(f"{p}:{key}", "v")
    samples = collect(service, keyspace, "-check-key-groups", f'"^({p}:[^,]+):%d$",^({p})')
    assert_samples(group_samples(samples, 10), expected(10, {f"{p}:alpha": 2, f"{p}:beta": 1, p: 2}, 3))


def test_overflow_keeps_groups_using_most_memory(service, keyspace):
    p, r = keyspace.prefix, keyspace.reserve(11)
    r.set(f"{p}:big:1", "x" * 20000)
    r.set(f"{p}:med:1", "x" * 5000)
    for i in range(5):
        r.set(f"{p}:many:{i}", "x")
    for group in ("s1", "s2"):
        r.set(f"{p}:{group}:1", "x")
        r.set(f"{p}:{group}:2", "x")
    samples = collect(service, keyspace, "-check-key-groups", f"^{p}:(%w+):", "-max-distinct-key-groups", "2")
    assert_samples(group_samples(samples, 11), expected(11, {"big": 1, "med": 1, "overflow": 9}, 5))
    memory = memory_by_group(samples, 11)
    assert memory["big"] > memory["med"]


def test_overflow_ties_on_memory_are_deterministic(service, keyspace):
    p, r = keyspace.prefix, keyspace.reserve(12)
    r.set(f"{p}:qq:1", "y" * 100)
    r.set(f"{p}:pp:1", "y" * 100)
    r.set(f"{p}:oo:1", "y")
    r.set(f"{p}:oo:2", "y")
    usage = {name: r.memory_usage(f"{p}:{name}") for name in ("pp:1", "qq:1", "oo:1", "oo:2")}
    assert usage["pp:1"] == usage["qq:1"] > usage["oo:1"] + usage["oo:2"], usage  # fixture precondition
    samples = collect(service, keyspace, "-check-key-groups", f"^{p}:(%a+):", "-max-distinct-key-groups", "1")
    assert_samples(group_samples(samples, 12), expected(12, {"pp": 1, "overflow": 3}, 3))


def test_overflow_memory_ties_prefer_fewer_keys(service, keyspace):
    p, r = keyspace.prefix, keyspace.reserve(7)
    r.set(f"{p}:aa:1", "y")
    r.set(f"{p}:aa:2", "y")
    target = r.memory_usage(f"{p}:aa:1") + r.memory_usage(f"{p}:aa:2")
    # Fixture construction: grow one zz value until the server reports the same total memory.
    for length in range(1, 512):
        r.set(f"{p}:zz:1", "y" * length)
        if r.memory_usage(f"{p}:zz:1") == target:
            break
    assert r.memory_usage(f"{p}:zz:1") == target, "no single value matches the two-key memory total"
    samples = collect(service, keyspace, "-check-key-groups", f"^{p}:(%a+):", "-max-distinct-key-groups", "1")
    assert_samples(group_samples(samples, 7), expected(7, {"zz": 1, "overflow": 2}, 2))


def test_zero_distinct_groups_puts_everything_in_overflow(service, keyspace):
    p, r = keyspace.prefix, keyspace.reserve(13)
    r.set(f"{p}:a:1", "v")
    r.set(f"{p}:a:2", "v")
    r.set(f"{p}:b:1", "v")
    samples = collect(service, keyspace, "-check-key-groups", f"^{p}:(%a+):", "-max-distinct-key-groups", "0")
    assert_samples(group_samples(samples, 13), expected(13, {"overflow": 3}, 2))
    memory_by_group(samples, 13)


def test_groups_aggregate_across_scan_batches(service, keyspace):
    p, r = keyspace.prefix, keyspace.reserve(14)
    for i in range(7):
        r.set(f"{p}:g{i % 3}:{i}", "v")
    samples = collect(service, keyspace, "-check-key-groups", f"^({p}:g%d):", "-check-keys-batch-size", "2")
    assert_samples(group_samples(samples, 14), expected(14, {f"{p}:g0": 3, f"{p}:g1": 2, f"{p}:g2": 2}, 3))


@pytest.mark.parametrize("groups", ["[a", 'a"b,^(x)'], ids=["bad-lua-pattern", "bad-csv"])
def test_invalid_group_list_reports_no_groups(service, keyspace, groups):
    p = keyspace.prefix
    keyspace.db(0).set(f"{p}:k", "5")
    samples = collect(service, keyspace, "-check-key-groups", groups, "-check-single-keys", f"{p}:k")
    names = {"key_group_count", GROUP_MEMORY, "number_of_distinct_key_groups"}
    assert [s for s in samples if s[0] in names] == []
    assert_samples(select(samples, mentions(p)), [
        ("key_size", {"db": "db0", "key": f"{p}:k"}, 1),
        ("key_value", {"db": "db0", "key": f"{p}:k"}, 5),
    ])
