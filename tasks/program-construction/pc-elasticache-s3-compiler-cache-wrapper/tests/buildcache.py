"""Helpers for driving the cache program and inspecting its Redis/S3 state."""

import io
import itertools
import json
import os
import random
import time
import uuid
import zipfile

import boto3
import redis
import zstandard
from harness import give_to_candidate, run_program, write_fixture

_ports = itertools.count(random.randrange(12000, 28000, 100))


def number(value):
    """Counters compare by numeric value; booleans are not counts."""
    assert type(value) in (int, float), f"expected a number, got {value!r}"
    return value


def lang_count(section, language="C/C++"):
    """`cache_hits`/`cache_misses` counts; an absent language key means zero."""
    counts = section["counts"]
    assert isinstance(counts, dict), counts
    return number(counts.get(language, 0))


def decode_entry(blob):
    """Entry values are ZIP archives whose members are each one zstd frame."""
    archive = zipfile.ZipFile(io.BytesIO(blob))
    members = {}
    for info in archive.infolist():
        members[info.filename] = zstandard.ZstdDecompressor().decompress(archive.read(info), max_output_size=1 << 26)
    return members


class Cache:
    """A private cache namespace: server port, Redis key prefix, S3 bucket and workspace."""

    def __init__(self, root, engine):
        self.root = root
        self.engine = engine
        tag = uuid.uuid4().hex[:12]
        self.port = next(_ports)
        self.prefix = f"bc{tag}/"
        self.bucket = f"bc-{tag}"
        self.s3 = boto3.client("s3")
        self.s3.create_bucket(Bucket=self.bucket)
        self.buckets = {self.bucket}
        self.prefixes = {self.prefix}
        self.ports = {self.port}
        self.dbs = {0}
        self.started = False
        for name in ("home", "tmp"):
            (root / name).mkdir()
            give_to_candidate(root / name)
        self.log = root / "server.log"

    # ----- configuration -------------------------------------------------
    def env(self, **overrides):
        env = {
            "HOME": str(self.root / "home"),
            "TMPDIR": str(self.root / "tmp"),
            "SCCACHE_SERVER_PORT": str(self.port),
            "SCCACHE_IDLE_TIMEOUT": "0",
            "SCCACHE_ERROR_LOG": str(self.log),
            "SCCACHE_MULTILEVEL_CHAIN": "redis,s3",
            "SCCACHE_REDIS_ENDPOINT": f"redis://{self.engine['host']}:{self.engine['port']}",
            "SCCACHE_REDIS_KEY_PREFIX": self.prefix,
            "SCCACHE_REDIS_TTL": "600",
            "SCCACHE_BUCKET": self.bucket,
            "SCCACHE_ENDPOINT": os.environ["AWS_ENDPOINT_URL"],
            "SCCACHE_REGION": "us-east-1",
        }
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = str(value)
        if "SCCACHE_REDIS_DB" in env:
            self.dbs.add(int(env["SCCACHE_REDIS_DB"]))
        if "SCCACHE_BUCKET" in env:
            self.buckets.add(env["SCCACHE_BUCKET"])
        self.ports.add(int(env["SCCACHE_SERVER_PORT"]))
        if env.get("SCCACHE_REDIS_KEY_PREFIX"):
            self.prefixes.add(env["SCCACHE_REDIS_KEY_PREFIX"])
        return env

    def run(self, *args, env=None, cwd=None, stdin=None, text=True, timeout=45):
        environment = self.env(**(env or {}))
        return run_program(list(args), cwd=cwd or self.root, stdin=stdin, env=environment, text=text, timeout=timeout)

    def start(self, **overrides):
        result = self.run("--start-server", env=overrides)
        self.started = True
        return result

    def stop(self, **overrides):
        return self.run("--stop-server", env=overrides)

    def restart(self, **overrides):
        self.stop()
        result = self.start(**overrides)
        assert result.returncode == 0, result.stderr
        return result

    def gcc(self, *args, env=None, cwd=None, stdin=None, check=True):
        self.started = True
        result = self.run("gcc", *args, env=env, cwd=cwd, stdin=stdin)
        if check:
            assert result.returncode == 0, f"compile failed: {result.stdout}\n{result.stderr}"
        return result

    def info(self):
        result = self.run("--show-stats", "--stats-format", "json")
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    def stats(self):
        return self.info()["stats"]

    def levels(self):
        return self.stats()["multi_level"]

    def zero(self):
        result = self.run("--zero-stats")
        assert result.returncode == 0, result.stderr
        return result

    # ----- fixtures ------------------------------------------------------
    def write(self, name, content, mode=0o644):
        path = self.root / name
        write_fixture(path, content, mode=mode)
        return path

    def read(self, name):
        return (self.root / name).read_bytes()

    def remove(self, name):
        (self.root / name).unlink()

    # ----- storage inspection -------------------------------------------
    def redis(self, db=0):
        return redis.Redis(host=self.engine["host"], port=self.engine["port"], db=db)

    @staticmethod
    def _is_entry(rest):
        return bool(rest) and not rest.rsplit("/", 1)[-1].startswith(".")

    def redis_keys(self, prefix=None, db=0):
        """All keys under a prefix (entries and health-check keys), mapped to TTL."""
        prefix = self.prefix if prefix is None else prefix
        client = self.redis(db)
        return {key.decode(): client.ttl(key) for key in client.scan_iter(match=prefix + "*", count=1000)}

    def entries(self, prefix=None, db=0):
        """Cache entries under the Redis prefix: key -> (value, ttl)."""
        prefix = self.prefix if prefix is None else prefix
        client = self.redis(db)
        found = {}
        for key in client.scan_iter(match=prefix + "*", count=1000):
            name = key.decode()
            if self._is_entry(name[len(prefix):]):
                found[name] = (client.get(key), client.ttl(key))
        return found

    def drop_entries(self, prefix=None, db=0):
        prefix = self.prefix if prefix is None else prefix
        client = self.redis(db)
        for key in list(client.scan_iter(match=prefix + "*", count=1000)):
            if self._is_entry(key.decode()[len(prefix):]):
                client.delete(key)

    def s3_keys(self, bucket=None):
        bucket = bucket or self.bucket
        keys = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
            keys.extend(item["Key"] for item in page.get("Contents", []))
        return keys

    def objects(self, prefix="", bucket=None):
        """Cache entries stored in S3 under a key prefix: key -> bytes."""
        bucket = bucket or self.bucket
        return {
            key: self.s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            for key in self.s3_keys(bucket)
            if key.startswith(prefix) and self._is_entry(key[len(prefix):])
        }

    def drop_objects(self, bucket=None):
        bucket = bucket or self.bucket
        for key in self.s3_keys(bucket):
            if self._is_entry(key):
                self.s3.delete_object(Bucket=bucket, Key=key)

    def cleanup(self):
        if self.started:
            for port in self.ports:
                try:
                    self.stop(SCCACHE_SERVER_PORT=port)
                except Exception:  # noqa: BLE001 -- teardown must continue to storage cleanup
                    pass
        for db in self.dbs:
            try:
                client = self.redis(db)
                for prefix in self.prefixes:
                    for key in list(client.scan_iter(match=prefix + "*", count=1000)):
                        client.delete(key)
            except redis.RedisError:
                pass
        for bucket in self.buckets:
            try:
                for key in self.s3_keys(bucket):
                    self.s3.delete_object(Bucket=bucket, Key=key)
                self.s3.delete_bucket(Bucket=bucket)
            except Exception:  # noqa: BLE001 -- the bucket may never have existed
                pass


def wait_for(predicate, timeout=15.0, interval=0.25):
    """Storage writes may land shortly after the client returns."""
    deadline = time.time() + timeout
    while True:
        value = predicate()
        if value or time.time() > deadline:
            return value
        time.sleep(interval)


GLOBAL_COUNTERS = (
    "compile_requests",
    "requests_not_compile",
    "requests_not_cacheable",
    "requests_executed",
    "requests_unsupported_compiler",
    "compilations",
    "compile_fails",
    "cache_writes",
    "cache_write_errors",
    "cache_read_errors",
    "forced_recaches",
)
LEVEL_COUNTERS = ("hits", "misses", "writes", "write_failures", "backfills_from", "backfills_to")


def counters(stats):
    """Documented global counters plus C hit/miss counts, compared by numeric value."""
    found = {name: number(stats[name]) for name in GLOBAL_COUNTERS}
    found["hits"] = lang_count(stats["cache_hits"])
    found["misses"] = lang_count(stats["cache_misses"])
    return found


def expect_counters(stats, **expected):
    """Every documented global counter: those not named are expected to be zero."""
    wanted = dict.fromkeys((*GLOBAL_COUNTERS, "hits", "misses"), 0)
    wanted.update(expected)
    assert counters(stats) == wanted


def level_counters(level):
    return {name: number(level[name]) for name in LEVEL_COUNTERS}


def expect_levels(stats, *expected):
    """Per-level counters in chain order; each expected item is (name, {counter: n})."""
    levels = stats["multi_level"]
    assert isinstance(levels, list) and len(levels) == len(expected), levels
    for level, (name, values) in zip(levels, expected):
        assert level["name"] == name
        wanted = dict.fromkeys(LEVEL_COUNTERS, 0)
        wanted.update(values)
        assert level_counters(level) == wanted, (name, level)


def not_cached(stats):
    reasons = stats["not_cached"]
    assert isinstance(reasons, dict), reasons
    return {reason: number(count) for reason, count in reasons.items() if count != 0}


def outcome(cache, *args, **options):
    """Compile once and classify the request as a cache 'hit' or 'miss'."""
    before = counters(cache.stats())
    cache.gcc(*args, **options)
    after = counters(cache.stats())
    delta = (after["hits"] - before["hits"], after["misses"] - before["misses"])
    assert delta in ((1, 0), (0, 1)), (args, before, after)
    return "hit" if delta == (1, 0) else "miss"


def fresh_port():
    """Another server port that no other test uses."""
    return next(_ports)
