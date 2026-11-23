"""Invocation, configuration sources, HTTP paths and startup/connection failures."""

import pytest

from conftest import free_port
from harness import run_program, write_fixture
from metrics import GRADED, assert_samples, mentions, select, value_of

IN_SCOPE_FLAGS = [
    "redis.addr", "web.listen-address", "web.telemetry-path", "namespace", "check-keys", "check-single-keys",
    "count-keys", "check-key-groups", "max-distinct-key-groups", "check-streams", "check-single-streams",
    "streams-exclude-consumer-metrics", "check-keys-batch-size", "disable-exporting-key-values", "script",
    "lua-script-read-only",
]


def test_help_lists_flags():
    completed = run_program(["-help"], timeout=20)
    assert completed.returncode == 0
    listing = completed.stdout + completed.stderr
    missing = [flag for flag in IN_SCOPE_FLAGS if f"-{flag}" not in listing]
    assert not missing, listing


INVALID_LISTS = {
    "non-numeric-db": ("-check-keys", "dbx=k:*"),
    "negative-db": ("-check-single-keys", "-1=k"),
    "two-equals": ("-check-streams", "db0=a=b"),
    "bad-escape": ("-count-keys", "k:%zz"),
    "single-stream-db": ("-check-single-streams", "dbq=s"),
}


@pytest.mark.parametrize("flag", INVALID_LISTS.values(), ids=INVALID_LISTS.keys())
def test_invalid_key_list_stops_startup(flag):
    completed = run_program(["-redis.addr", "redis://127.0.0.1:1", "-web.listen-address", f"127.0.0.1:{free_port()}", *flag],
                            timeout=20)
    assert completed.returncode != 0


def test_missing_script_file_stops_startup(scratch):
    completed = run_program(["-redis.addr", "redis://127.0.0.1:1", "-web.listen-address", f"127.0.0.1:{free_port()}",
                             "-script", "absent.lua"], cwd=scratch, timeout=20)
    assert completed.returncode != 0


def test_namespace_and_telemetry_path(service, keyspace):
    p = keyspace.prefix
    keyspace.db(0).set(f"{p}:k", "12")
    running = service("-redis.addr", keyspace.engine.url, "-namespace", "kg", "-web.telemetry-path", "/m",
                      "-check-single-keys", f"{p}:k")
    status, body = running.get("/health")
    assert (status, body.strip()) == (200, "ok")
    assert running.get("/metrics")[0] == 404
    samples = running.samples("/m", namespace="kg")
    assert value_of(samples, "up") == 1
    assert_samples(select(samples, mentions(p)), [
        ("key_size", {"db": "db0", "key": f"{p}:k"}, 2), ("key_value", {"db": "db0", "key": f"{p}:k"}, 12),
    ])
    assert select(running.samples("/m", namespace="redis"), lambda s: True) == []


def test_environment_configuration_and_flag_precedence(service, keyspace):
    p = keyspace.prefix
    keyspace.db(0).set(f"{p}:a", "1")
    keyspace.db(0).set(f"{p}:b", "two")
    env = {
        "REDIS_ADDR": keyspace.engine.url,
        "REDIS_EXPORTER_CHECK_SINGLE_KEYS": f"{p}:a",
        "REDIS_EXPORTER_DISABLE_EXPORTING_KEY_VALUES": "true",
    }
    samples = service(env=env).samples()
    assert value_of(samples, "up") == 1
    assert_samples(select(samples, mentions(p)), [("key_size", {"db": "db0", "key": f"{p}:a"}, 1)])

    samples = service("-check-single-keys", f"{p}:b", env=env).samples()
    assert_samples(select(samples, mentions(p)), [("key_size", {"db": "db0", "key": f"{p}:b"}, 3)])

    samples = service("-check-single-keys", f"{p}:b", "-redis.addr", "redis://127.0.0.1:1", env=env).samples()
    assert value_of(samples, "up") == 0


def test_unreachable_server_reports_down(service, scratch):
    write_fixture(scratch / "s.lua", 'return {"v", "1"}', mode=0o644)
    running = service("-redis.addr", "redis://127.0.0.1:1", "-check-keys", "k:*", "-check-single-keys", "k",
                      "-count-keys", "k:*", "-check-key-groups", "^(k)", "-check-single-streams", "s",
                      "-script", "s.lua", cwd=scratch)
    status, _ = running.get("/metrics")
    assert status == 200
    assert_samples(select(running.samples(), lambda s: s[0] in GRADED), [("up", {}, 0)])
