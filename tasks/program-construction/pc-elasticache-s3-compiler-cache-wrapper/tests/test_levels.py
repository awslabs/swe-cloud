"""The two-level storage chain: lookup order, write-through, backfill, read-only levels, TTL and failures."""

import time

import pytest
from buildcache import decode_entry, expect_counters, expect_levels, lang_count, number, wait_for

ONE = "int one(int x) { return x + 1; }\n"
TWO = "int two(int x) { return x * 2; }\n"
THREE = "int three(int x) { return x - 3; }\n"


def sources(cache):
    for name, text in (("one", ONE), ("two", TWO), ("three", THREE)):
        cache.write(f"{name}.c", text)


def decoded(values):
    """Entry contents independent of key names, keeping multiplicity."""
    return sorted(sorted(decode_entry(value).items()) for value in values)


def redis_values(cache, **options):
    return [value for value, _ in cache.entries(**options).values()]


def settle(cache, objects):
    return wait_for(lambda: len(cache.objects()) == objects and cache.objects())


def test_second_level_hit_backfills_first_level(cache):
    cache.start()
    sources(cache)
    cache.gcc("-c", "one.c", "-o", "one.o")
    original = cache.read("one.o")
    stored = settle(cache, 1)
    assert len(stored) == 1
    cache.restart()
    cache.drop_entries()
    cache.remove("one.o")

    cache.gcc("-c", "one.c", "-o", "one.o")
    assert cache.read("one.o") == original
    expect_counters(cache.stats(), compile_requests=1, requests_executed=1, hits=1)
    refilled = wait_for(lambda: cache.entries())
    assert len(refilled) == 1
    (value, ttl), = refilled.values()
    assert 590 <= ttl <= 600, "a backfilled entry gets the configured TTL afresh"
    assert decoded([value]) == decoded(stored.values())
    wait_for(lambda: number(cache.levels()[0]["backfills_to"]) == 1)
    expect_levels(cache.stats(), ("L0 (redis)", {"misses": 1, "backfills_to": 1}), ("L1 (s3)", {"hits": 1, "backfills_from": 1}))

    cache.gcc("-c", "one.c", "-o", "again.o")
    assert cache.read("again.o") == original
    expect_levels(cache.stats(), ("L0 (redis)", {"hits": 1, "misses": 1, "backfills_to": 1}), ("L1 (s3)", {"hits": 1, "backfills_from": 1}))
    assert cache.objects() == stored


def test_first_level_hit_does_not_rewrite_second_level(cache):
    cache.start()
    sources(cache)
    cache.gcc("-c", "one.c", "-o", "one.o")
    cache.gcc("-c", "two.c", "-o", "two.o")
    settle(cache, 2)
    cache.drop_objects()
    cache.gcc("-c", "one.c", "-o", "one-again.o")
    cache.gcc("-c", "two.c", "-o", "two-again.o")
    time.sleep(2)
    assert cache.objects() == {}
    assert len(cache.entries()) == 2
    expect_counters(cache.stats(), compile_requests=4, requests_executed=4, compilations=2, cache_writes=2, hits=2, misses=2)


def test_lost_entries_are_rebuilt_in_both_levels(cache):
    cache.start()
    sources(cache)
    cache.gcc("-c", "one.c", "-o", "one.o")
    original = cache.read("one.o")
    settle(cache, 1)
    cache.drop_entries()
    cache.drop_objects()
    cache.gcc("-c", "one.c", "-o", "rebuilt.o")
    assert cache.read("rebuilt.o") == original
    assert len(cache.entries()) == 1
    assert len(settle(cache, 1)) == 1
    expect_counters(cache.stats(), compile_requests=2, requests_executed=2, compilations=2, cache_writes=2, misses=2)
    wait_for(lambda: number(cache.levels()[1]["writes"]) == 2)
    expect_levels(cache.stats(), ("L0 (redis)", {"misses": 2, "writes": 2}), ("L1 (s3)", {"misses": 2, "writes": 2}))


def test_expired_first_level_entry_is_refilled_from_s3(cache):
    cache.start(SCCACHE_REDIS_TTL=2)
    sources(cache)
    cache.gcc("-c", "one.c", "-o", "one.o")
    (_, ttl), = cache.entries().values()
    assert 0 < ttl <= 2
    settle(cache, 1)
    assert wait_for(lambda: cache.entries() == {}, timeout=10), "the entry should expire"
    cache.gcc("-c", "one.c", "-o", "again.o")
    assert cache.read("again.o") == cache.read("one.o")
    expect_counters(cache.stats(), compile_requests=2, requests_executed=2, compilations=1, cache_writes=1, hits=1, misses=1)
    refilled = wait_for(lambda: cache.entries())
    assert len(refilled) == 1
    assert all(0 < ttl <= 2 for _, ttl in refilled.values())
    assert number(cache.levels()[1]["hits"]) == 1


def test_ttl_unset_stores_entries_without_expiry(cache):
    cache.start(SCCACHE_REDIS_TTL=None)
    sources(cache)
    cache.gcc("-c", "one.c", "-o", "one.o")
    entries = cache.entries()
    assert len(entries) == 1
    assert [ttl for _, ttl in entries.values()] == [-1]
    assert all(ttl in (-1,) for ttl in cache.redis_keys().values())


def test_expiration_variable_sets_the_ttl(cache):
    cache.start(SCCACHE_REDIS_TTL=None, SCCACHE_REDIS_EXPIRATION=300)
    sources(cache)
    cache.gcc("-c", "one.c", "-o", "one.o", env={"SCCACHE_REDIS_TTL": None, "SCCACHE_REDIS_EXPIRATION": 300})
    entries = cache.entries()
    assert len(entries) == 1
    assert all(290 <= ttl <= 300 for _, ttl in entries.values())


def test_read_only_redis_level(cache):
    cache.start()
    sources(cache)
    cache.gcc("-c", "one.c", "-o", "one.o")
    settle(cache, 1)
    cache.restart(SCCACHE_REDIS_RW_MODE="READ_ONLY")
    assert cache.info()["effective_cache_mode"] == "ReadWrite"
    redis_before = {key: value for key, (value, _) in cache.entries().items()}

    cache.gcc("-c", "one.c", "-o", "one-again.o")
    assert cache.read("one-again.o") == cache.read("one.o")
    cache.gcc("-c", "two.c", "-o", "two.o")
    objects = settle(cache, 2)
    assert len(objects) == 2
    cache.remove("two.o")
    cache.gcc("-c", "two.c", "-o", "two.o")
    time.sleep(2)
    assert {key: value for key, (value, _) in cache.entries().items()} == redis_before
    expect_counters(cache.stats(), compile_requests=3, requests_executed=3, compilations=1, cache_writes=1, hits=2, misses=1)
    expect_levels(
        cache.stats(),
        ("L0 (redis)", {"hits": 1, "misses": 2}),
        ("L1 (s3)", {"hits": 1, "misses": 1, "writes": 1, "backfills_from": 1}),
    )


def test_fresh_read_only_redis_is_never_written(cache):
    cache.start(SCCACHE_REDIS_RW_MODE="READ_ONLY")
    sources(cache)
    cache.gcc("-c", "one.c", "-o", "one.o")
    assert len(settle(cache, 1)) == 1
    time.sleep(1)
    assert cache.redis_keys() == {}, "not even a health-check key"


def test_read_only_s3_level(cache):
    cache.start(SCCACHE_S3_RW_MODE="READ_ONLY")
    sources(cache)
    cache.gcc("-c", "one.c", "-o", "one.o")
    assert len(cache.entries()) == 1
    time.sleep(2)
    assert cache.s3_keys() == [], "a read-only bucket is never written"

    cache.restart()
    cache.gcc("-c", "two.c", "-o", "two.o")
    stored = settle(cache, 1)
    cache.restart(SCCACHE_S3_RW_MODE="READ_ONLY")
    assert cache.info()["effective_cache_mode"] == "ReadWrite"
    bucket_before = {key: cache.s3.get_object(Bucket=cache.bucket, Key=key)["Body"].read() for key in cache.s3_keys()}
    cache.drop_entries()
    cache.remove("two.o")
    cache.gcc("-c", "two.c", "-o", "two.o")
    refilled = wait_for(lambda: cache.entries())
    assert len(refilled) == 1
    assert decoded(v for v, _ in refilled.values()) == decoded(stored.values())
    cache.gcc("-c", "three.c", "-o", "three.o")
    assert len(cache.entries()) == 2
    time.sleep(2)
    assert {key: cache.s3.get_object(Bucket=cache.bucket, Key=key)["Body"].read() for key in cache.s3_keys()} == bucket_before
    wait_for(lambda: number(cache.levels()[0]["backfills_to"]) == 1)
    expect_counters(cache.stats(), compile_requests=2, requests_executed=2, compilations=1, cache_writes=1, hits=1, misses=1)
    expect_levels(
        cache.stats(),
        ("L0 (redis)", {"misses": 2, "writes": 1, "backfills_to": 1}),
        ("L1 (s3)", {"hits": 1, "misses": 1, "backfills_from": 1}),
    )


def test_fully_read_only_chain_serves_hits_and_stores_nothing(cache):
    cache.start()
    sources(cache)
    cache.gcc("-c", "one.c", "-o", "one.o")
    settle(cache, 1)
    cache.restart(SCCACHE_REDIS_RW_MODE="READ_ONLY", SCCACHE_S3_RW_MODE="READ_ONLY")
    assert cache.info()["effective_cache_mode"] == "ReadOnly"
    redis_before = {key: value for key, (value, _) in cache.entries().items()}
    s3_before = cache.objects()
    cache.gcc("-c", "one.c", "-o", "one-again.o")
    assert cache.read("one-again.o") == cache.read("one.o")
    cache.gcc("-c", "two.c", "-o", "two.o")
    assert (cache.root / "two.o").stat().st_size > 0
    time.sleep(2)
    assert {key: value for key, (value, _) in cache.entries().items()} == redis_before
    assert cache.objects() == s3_before
    stats = cache.stats()
    assert number(stats["cache_writes"]) == 0
    assert number(stats["compilations"]) == 1
    assert lang_count(stats["cache_hits"]) == 1
    assert lang_count(stats["cache_misses"]) == 1


def test_chain_order_is_lookup_order(cache):
    cache.start(SCCACHE_MULTILEVEL_CHAIN="s3,redis")
    sources(cache)
    cache.gcc("-c", "one.c", "-o", "one.o", env={"SCCACHE_MULTILEVEL_CHAIN": "s3,redis"})
    stored = settle(cache, 1)
    assert len(wait_for(lambda: cache.entries())) == 1
    wait_for(lambda: number(cache.levels()[1]["writes"]) == 1)
    cache.drop_objects()
    cache.gcc("-c", "one.c", "-o", "again.o", env={"SCCACHE_MULTILEVEL_CHAIN": "s3,redis"})
    assert cache.read("again.o") == cache.read("one.o")
    refilled = settle(cache, 1)
    assert decoded(refilled.values()) == decoded(stored.values())
    wait_for(lambda: number(cache.levels()[0]["backfills_to"]) == 1)
    expect_levels(
        cache.stats(),
        ("L0 (s3)", {"misses": 2, "writes": 1, "backfills_to": 1}),
        ("L1 (redis)", {"hits": 1, "misses": 1, "writes": 1, "backfills_from": 1}),
    )
    assert cache.info()["cache_location"] == "Multi-level (2 levels)"


def test_single_level_chain(cache):
    cache.start(SCCACHE_MULTILEVEL_CHAIN="redis")
    sources(cache)
    cache.gcc("-c", "one.c", "-o", "one.o", env={"SCCACHE_MULTILEVEL_CHAIN": "redis"})
    cache.gcc("-c", "one.c", "-o", "two.o", env={"SCCACHE_MULTILEVEL_CHAIN": "redis"})
    info = cache.info()
    assert info["cache_location"] == "Multi-level (1 levels)"
    expect_levels(info["stats"], ("L0 (redis)", {"hits": 1, "misses": 1, "writes": 1}))
    assert len(cache.entries()) == 1
    time.sleep(1)
    assert cache.s3_keys() == []


def test_without_a_chain_one_backend_is_used(cache):
    redis_only = {"SCCACHE_MULTILEVEL_CHAIN": None, "SCCACHE_BUCKET": None}
    cache.start(**redis_only)
    sources(cache)
    cache.gcc("-c", "one.c", "-o", "one.o", env=redis_only)
    info = cache.info()
    assert not info["stats"].get("multi_level")
    assert "Multi-level" not in info["cache_location"]
    assert len(cache.entries()) == 1
    assert cache.s3_keys() == []

    both = {"SCCACHE_MULTILEVEL_CHAIN": None}
    cache.restart(**both)
    cache.drop_entries()
    cache.gcc("-c", "two.c", "-o", "two.o", env=both)
    assert len(settle(cache, 1)) == 1
    assert cache.entries() == {}, "with both backends configured and no chain, S3 is the one used"


def test_redis_database_selection(cache):
    cache.start(SCCACHE_REDIS_DB=3)
    sources(cache)
    cache.gcc("-c", "one.c", "-o", "one.o", env={"SCCACHE_REDIS_DB": 3})
    assert len(cache.entries(db=3)) == 1
    assert cache.redis_keys(db=0) == {}
    cache.gcc("-c", "one.c", "-o", "again.o", env={"SCCACHE_REDIS_DB": 3})
    assert number(cache.levels()[0]["hits"]) == 1


def test_s3_key_prefix(cache):
    cache.start(SCCACHE_S3_KEY_PREFIX="objs/")
    sources(cache)
    cache.gcc("-c", "one.c", "-o", "one.o", env={"SCCACHE_S3_KEY_PREFIX": "objs/"})
    stored = wait_for(lambda: cache.objects(prefix="objs/"))
    assert len(stored) == 1
    assert all(key.startswith("objs/") for key in cache.s3_keys())
    cache.restart(SCCACHE_S3_KEY_PREFIX="objs/")
    cache.drop_entries()
    cache.gcc("-c", "one.c", "-o", "again.o", env={"SCCACHE_S3_KEY_PREFIX": "objs/"})
    assert cache.read("again.o") == cache.read("one.o")
    assert number(cache.levels()[1]["hits"]) == 1
    # The same bucket under another key prefix is a different cache.
    cache.restart(SCCACHE_S3_KEY_PREFIX="other/")
    cache.drop_entries()
    cache.gcc("-c", "one.c", "-o", "third.o", env={"SCCACHE_S3_KEY_PREFIX": "other/"})
    expect_counters(cache.stats(), compile_requests=1, requests_executed=1, compilations=1, cache_writes=1, misses=1)
    assert len(wait_for(lambda: cache.objects(prefix="other/"))) == 1
    assert cache.objects(prefix="objs/") == stored


@pytest.mark.parametrize("policy", [None, "l0"])
def test_unreachable_first_level_prevents_start_by_default(cache, policy):
    settings = {"SCCACHE_REDIS_ENDPOINT": f"redis://{cache.engine['host']}:1", "SCCACHE_MULTILEVEL_WRITE_ERROR_POLICY": policy}
    started = cache.start(**settings)
    assert started.returncode != 0
    assert cache.s3_keys() == []
    assert cache.stop().returncode != 0


@pytest.mark.timeout(150)  # the reference retries the refused Redis connection for ~10 s per operation
def test_ignore_policy_tolerates_unreachable_first_level(cache):
    settings = {"SCCACHE_REDIS_ENDPOINT": f"redis://{cache.engine['host']}:1", "SCCACHE_MULTILEVEL_WRITE_ERROR_POLICY": "ignore"}
    started = cache.start(**settings)
    assert started.returncode == 0, started.stderr
    sources(cache)
    cache.gcc("-c", "one.c", "-o", "one.o", env=settings)
    assert len(settle(cache, 1)) == 1
    cache.gcc("-c", "one.c", "-o", "again.o", env=settings)
    assert cache.read("again.o") == cache.read("one.o")
    expect_counters(cache.stats(), compile_requests=2, requests_executed=2, compilations=1, cache_writes=1, hits=1, misses=1)
    assert wait_for(lambda: number(cache.levels()[0]["write_failures"]) == 1)
    expect_levels(
        cache.stats(),
        ("L0 (redis)", {"misses": 2, "write_failures": 1}),
        ("L1 (s3)", {"hits": 1, "misses": 1, "writes": 1, "backfills_from": 1}),
    )


def test_missing_bucket_policies(cache):
    absent = {"SCCACHE_BUCKET": cache.bucket + "-absent"}
    started = cache.start(**absent)
    assert started.returncode == 0, started.stderr
    sources(cache)
    cache.gcc("-c", "one.c", "-o", "one.o", env=absent)
    cache.gcc("-c", "one.c", "-o", "again.o", env=absent)
    assert len(cache.entries()) == 1
    expect_counters(cache.stats(), compile_requests=2, requests_executed=2, compilations=1, cache_writes=1, hits=1, misses=1)
    assert wait_for(lambda: number(cache.levels()[1]["write_failures"]) == 1)
    expect_levels(cache.stats(), ("L0 (redis)", {"hits": 1, "misses": 1, "writes": 1}), ("L1 (s3)", {"misses": 1, "write_failures": 1}))
    cache.stop()
    strict = cache.start(SCCACHE_MULTILEVEL_WRITE_ERROR_POLICY="all", **absent)
    assert strict.returncode != 0
    assert cache.stop().returncode != 0


@pytest.mark.parametrize(("policy", "writes", "errors"), [("l0", 1, 0), ("all", 0, 1), ("ignore", 1, 0)])
def test_write_failure_after_start_follows_policy(cache, policy, writes, errors):
    assert cache.start(SCCACHE_MULTILEVEL_WRITE_ERROR_POLICY=policy).returncode == 0
    for key in cache.s3_keys():
        cache.s3.delete_object(Bucket=cache.bucket, Key=key)
    cache.s3.delete_bucket(Bucket=cache.bucket)
    sources(cache)
    result = cache.gcc("-c", "one.c", "-o", "one.o")
    assert result.stderr == ""
    assert (cache.root / "one.o").stat().st_size > 0
    assert len(wait_for(lambda: cache.entries())) == 1
    assert wait_for(lambda: number(cache.levels()[1]["write_failures"]) == 1)
    expect_counters(cache.stats(), compile_requests=1, requests_executed=1, compilations=1, cache_writes=writes, cache_write_errors=errors, misses=1)
    expect_levels(cache.stats(), ("L0 (redis)", {"misses": 1, "writes": 1}), ("L1 (s3)", {"misses": 1, "write_failures": 1}))
    cache.gcc("-c", "one.c", "-o", "again.o")
    assert number(cache.levels()[0]["hits"]) == 1


def test_recache_is_requested_by_the_client(cache):
    cache.start(SCCACHE_RECACHE=1)
    sources(cache)
    cache.gcc("-c", "one.c", "-o", "one.o")
    cache.gcc("-c", "one.c", "-o", "two.o")
    expect_counters(cache.stats(), compile_requests=2, requests_executed=2, compilations=1, cache_writes=1, hits=1, misses=1)
    settle(cache, 1)
    cache.drop_objects()
    cache.gcc("-c", "one.c", "-o", "three.o", env={"SCCACHE_RECACHE": 1})
    assert cache.read("three.o") == cache.read("one.o")
    expect_counters(cache.stats(), compile_requests=3, requests_executed=3, compilations=2, cache_writes=2, hits=1, misses=2, forced_recaches=1)
    assert len(settle(cache, 1)) == 1, "a forced recache rewrites every level"
    assert len(cache.entries()) == 1
    wait_for(lambda: number(cache.levels()[1]["writes"]) == 2)
    expect_levels(cache.stats(), ("L0 (redis)", {"hits": 1, "misses": 1, "writes": 2}), ("L1 (s3)", {"misses": 1, "writes": 2}))
