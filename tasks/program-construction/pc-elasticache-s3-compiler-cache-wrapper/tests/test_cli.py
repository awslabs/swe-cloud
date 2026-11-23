"""Invocation, server lifecycle and statistics reporting of the public executable."""

import re
import time

from buildcache import expect_counters, expect_levels, not_cached, number, wait_for

SOURCE = "int scale(int x) { return x * 9 + 1; }\n"
WARNING = "int noisy(void) { int unused = 3; return 0; }\n"


def stat_line(text, label):
    """Value of a `Label    value` row in the text statistics report."""
    match = re.search(rf"^{re.escape(label)}[ \t]+(\S+)[ \t]*$", text, re.MULTILINE)
    assert match, f"no {label!r} row in:\n{text}"
    return match.group(1)


def test_version_matches_reported_version(cache):
    result = cache.run("--version")
    assert result.returncode == 0, result.stderr
    words = result.stdout.split()
    assert words and words[-1] == "0.19.0"
    assert cache.info()["version"] == "0.19.0"


def test_fresh_server_reports_zeroed_chain(cache):
    started = cache.start()
    assert started.returncode == 0, started.stderr
    info = cache.info()
    assert info["cache_location"] == "Multi-level (2 levels)"
    assert info["effective_cache_mode"] == "ReadWrite"
    stats = info["stats"]
    expect_counters(stats)
    assert not_cached(stats) == {}
    expect_levels(stats, ("L0 (redis)", {}), ("L1 (s3)", {}))
    for level in stats["multi_level"]:
        assert type(level["name"]) is str


def test_server_lifecycle_exit_statuses(cache):
    assert cache.start().returncode == 0
    second = cache.start()
    assert second.returncode != 0, "a second server cannot bind the same port"
    expect_counters(cache.stats())  # the first server keeps running
    stopped = cache.stop()
    assert stopped.returncode == 0, stopped.stderr
    again = cache.stop()
    assert again.returncode != 0, "stopping without a server must fail"


def test_compile_starts_server_on_demand(cache):
    cache.write("scale.c", SOURCE)
    cache.gcc("-c", "scale.c", "-o", "scale.o")
    assert (cache.root / "scale.o").stat().st_size > 0
    stats = cache.stats()
    expect_counters(stats, compile_requests=1, requests_executed=1, compilations=1, cache_writes=1, misses=1)
    assert cache.stop().returncode == 0, "the compile left a running server behind"


def test_zero_stats_resets_global_counters_only(cache):
    cache.start()
    cache.write("scale.c", SOURCE)
    cache.gcc("-c", "scale.c", "-o", "a.o")
    cache.gcc("-c", "scale.c", "-o", "b.o")
    before = cache.stats()
    expect_counters(before, compile_requests=2, requests_executed=2, compilations=1, cache_writes=1, hits=1, misses=1)
    wait_for(lambda: number(cache.levels()[1]["writes"]) == 1)
    zeroed = cache.zero()
    assert zeroed.returncode == 0
    stats = cache.stats()
    expect_counters(stats)
    assert not_cached(stats) == {}
    # Per-level counters belong to the storage chain and survive --zero-stats.
    expect_levels(stats, ("L0 (redis)", {"hits": 1, "misses": 1, "writes": 1}), ("L1 (s3)", {"misses": 1, "writes": 1}))
    cache.write("other.c", WARNING)
    cache.gcc("-c", "other.c", "-o", "other.o")
    stats = cache.stats()
    expect_counters(stats, compile_requests=1, requests_executed=1, compilations=1, cache_writes=1, misses=1)
    assert number(stats["multi_level"][0]["misses"]) == 2


def test_restart_loses_statistics_but_keeps_entries(cache):
    cache.start()
    cache.write("scale.c", SOURCE)
    cache.gcc("-c", "scale.c", "-o", "a.o")
    stopped = cache.stop()
    assert stopped.returncode == 0
    assert stat_line(stopped.stdout, "Compile requests") == "1"
    assert stat_line(stopped.stdout, "Cache misses") == "1"
    assert cache.start().returncode == 0
    expect_counters(cache.stats())
    expect_levels(cache.stats(), ("L0 (redis)", {}), ("L1 (s3)", {}))
    cache.gcc("-c", "scale.c", "-o", "b.o")
    assert cache.read("a.o") == cache.read("b.o")
    expect_counters(cache.stats(), compile_requests=1, requests_executed=1, hits=1)


def test_text_statistics_report(cache):
    cache.start()
    cache.write("scale.c", SOURCE)
    cache.gcc("-c", "scale.c", "-o", "a.o")
    cache.gcc("-c", "scale.c", "-o", "b.o")
    cache.gcc("-c", "scale.c", "-o", "c.o")
    for args in (("--show-stats",), ("--show-stats", "--stats-format", "text")):
        result = cache.run(*args)
        assert result.returncode == 0, result.stderr
        assert stat_line(result.stdout, "Compile requests") == "3"
        assert stat_line(result.stdout, "Cache hits") == "2"
        assert stat_line(result.stdout, "Cache misses") == "1"
        assert stat_line(result.stdout, "Compilations") == "1"


def test_idle_timeout_shuts_server_down(cache):
    assert cache.start(SCCACHE_IDLE_TIMEOUT=2).returncode == 0
    cache.write("scale.c", SOURCE)
    cache.gcc("-c", "scale.c", "-o", "a.o", env={"SCCACHE_IDLE_TIMEOUT": 2})
    assert len(wait_for(lambda: cache.entries())) == 1
    time.sleep(6)
    assert cache.stop().returncode != 0, "the idle server should have exited"
    assert len(cache.entries()) == 1


def test_conflicting_expiration_settings_are_rejected(cache):
    both = {"SCCACHE_REDIS_TTL": 100, "SCCACHE_REDIS_EXPIRATION": 300}
    started = cache.start(**both)
    assert started.returncode != 0
    assert started.stderr.strip()
    cache.write("scale.c", SOURCE)
    compiled = cache.gcc("-c", "scale.c", "-o", "a.o", env=both, check=False)
    assert compiled.returncode != 0
    assert compiled.stderr.strip()
    assert cache.stop(**both).returncode != 0, "no server may be running"
    assert cache.redis_keys() == {}
    assert cache.s3_keys() == []


def test_running_server_ignores_client_storage_settings(cache):
    cache.start()
    cache.write("scale.c", SOURCE)
    elsewhere = {"SCCACHE_REDIS_KEY_PREFIX": "elsewhere-" + cache.prefix, "SCCACHE_BUCKET": cache.bucket + "-absent", "SCCACHE_REDIS_TTL": 5}
    cache.gcc("-c", "scale.c", "-o", "a.o", env=elsewhere)
    entries = wait_for(lambda: cache.entries())
    assert len(entries) == 1
    assert all(ttl > 100 for _, ttl in entries.values()), "the server's TTL applies"
    assert cache.redis_keys(prefix="elsewhere-" + cache.prefix) == {}
    assert len(wait_for(lambda: cache.objects())) == 1
