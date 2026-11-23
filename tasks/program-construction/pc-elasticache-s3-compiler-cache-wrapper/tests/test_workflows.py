"""Multi-step build scenarios across edits, servers and partially lost storage."""

import json
import time

from buildcache import decode_entry, expect_counters, expect_levels, fresh_port, number, wait_for
from harness import give_to_candidate, write_fixture

PROJECT = {
    "common.h": "#define SCALE 3\nint helper(int);\n",
    "a.c": '#include "common.h"\nint a(int x) { return helper(x) * SCALE; }\n',
    "b.c": '#include "common.h"\nint b(int x) { return helper(x) + SCALE; }\n',
    "c.c": "int helper(int x) { return x - 1; }\n",
}
UNITS = ("a", "b", "c")


def checkout(cache, name):
    """A separate copy of the project tree, as on another build machine."""
    directory = cache.root / name
    directory.mkdir()
    give_to_candidate(directory)
    for file, text in PROJECT.items():
        write_fixture(directory / file, text)
    return directory


def build(cache, directory, suffix="", port=None, env=None):
    overrides = dict(env or {})
    if port is not None:
        overrides["SCCACHE_SERVER_PORT"] = port
    for unit in UNITS:
        cache.gcc("-O1", "-c", f"{unit}.c", "-o", f"{unit}{suffix}.o", cwd=directory, env=overrides)
    return {unit: (directory / f"{unit}{suffix}.o").read_bytes() for unit in UNITS}


def decoded(values):
    return sorted(sorted(decode_entry(value).items()) for value in values)


def stats_on(cache, port):
    return cache.run("--show-stats", "--stats-format", "json", env={"SCCACHE_SERVER_PORT": port})


def test_header_edit_rebuilds_only_dependent_units(cache):
    cache.start()
    tree = checkout(cache, "tree")
    first = build(cache, tree, ".1")
    expect_counters(cache.stats(), compile_requests=3, requests_executed=3, compilations=3, cache_writes=3, misses=3)

    write_fixture(tree / "common.h", PROJECT["common.h"].replace("SCALE 3", "SCALE 4"))
    edited = build(cache, tree, ".2")
    assert edited["c"] == first["c"]
    assert edited["a"] != first["a"] and edited["b"] != first["b"]
    expect_counters(cache.stats(), compile_requests=6, requests_executed=6, compilations=5, cache_writes=5, hits=1, misses=5)

    write_fixture(tree / "common.h", PROJECT["common.h"])
    reverted = build(cache, tree, ".3")
    assert reverted == first
    expect_counters(cache.stats(), compile_requests=9, requests_executed=9, compilations=5, cache_writes=5, hits=4, misses=5)
    assert len(cache.entries()) == 5
    assert len(wait_for(lambda: len(cache.objects()) == 5 and cache.objects())) == 5


def test_servers_on_other_machines_share_the_storage(cache):
    cache.start()
    first = build(cache, checkout(cache, "machine-a"))
    stored_objects = wait_for(lambda: len(cache.objects()) == 3 and cache.objects())
    stored_entries = {key: value for key, (value, _) in cache.entries().items()}
    assert len(stored_entries) == 3
    cache.stop()

    # Another server with the same Redis prefix and bucket hits in Redis.
    second_port = fresh_port()
    assert cache.start(SCCACHE_SERVER_PORT=second_port).returncode == 0
    assert build(cache, checkout(cache, "machine-b"), port=second_port) == first
    second = stats_on(cache, second_port)
    assert second.returncode == 0

    # A server with a fresh Redis prefix but the shared bucket hits in S3 and fills its own prefix.
    third_port = fresh_port()
    own_prefix = "own-" + cache.prefix
    third_env = {"SCCACHE_SERVER_PORT": third_port, "SCCACHE_REDIS_KEY_PREFIX": own_prefix}
    assert cache.start(**third_env).returncode == 0
    assert build(cache, checkout(cache, "machine-c"), env=third_env) == first
    refilled = wait_for(lambda: len(cache.entries(prefix=own_prefix)) == 3 and cache.entries(prefix=own_prefix))
    assert len(refilled) == 3
    assert decoded(value for value, _ in refilled.values()) == decoded(stored_entries.values())
    assert {key: value for key, (value, _) in cache.entries().items()} == stored_entries
    assert cache.objects() == stored_objects

    second_stats = json.loads(second.stdout)["stats"]
    expect_counters(second_stats, compile_requests=3, requests_executed=3, hits=3)
    expect_levels(second_stats, ("L0 (redis)", {"hits": 3}), ("L1 (s3)", {}))
    third = json.loads(stats_on(cache, third_port).stdout)["stats"]
    expect_counters(third, compile_requests=3, requests_executed=3, hits=3)
    wait_for(lambda: number(json.loads(stats_on(cache, third_port).stdout)["stats"]["multi_level"][0]["backfills_to"]) == 3)
    third = json.loads(stats_on(cache, third_port).stdout)["stats"]
    expect_levels(third, ("L0 (redis)", {"misses": 3, "backfills_to": 3}), ("L1 (s3)", {"hits": 3, "backfills_from": 3}))


def test_partial_storage_loss_is_served_by_the_surviving_level(cache):
    cache.start()
    tree = checkout(cache, "tree")
    first = build(cache, tree, ".1")
    objects = wait_for(lambda: len(cache.objects()) == 3 and cache.objects())
    entries = {key: value for key, (value, _) in cache.entries().items()}
    cache.restart()

    # Lose a.o's entry from Redis and b.o's from S3 (matched by content); c.o survives in both.
    redis_client = cache.redis()
    for key, value in entries.items():
        if first["a"] in decode_entry(value).values():
            redis_client.delete(key)
    for key, value in objects.items():
        if first["b"] in decode_entry(value).values():
            cache.s3.delete_object(Bucket=cache.bucket, Key=key)
    assert len(cache.entries()) == 2 and len(cache.objects()) == 2

    assert build(cache, tree, ".2") == first
    expect_counters(cache.stats(), compile_requests=3, requests_executed=3, hits=3)
    wait_for(lambda: number(cache.levels()[0]["backfills_to"]) == 1)
    expect_levels(cache.stats(), ("L0 (redis)", {"hits": 2, "misses": 1, "backfills_to": 1}), ("L1 (s3)", {"hits": 1, "backfills_from": 1}))
    time.sleep(1)
    assert len(cache.entries()) == 3, "the Redis copy is restored from S3"
    assert len(cache.objects()) == 2, "a Redis hit does not restore the S3 copy"
