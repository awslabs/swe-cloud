"""Configuration loading: validation, file layout, merging and live reloads."""

import time

import pytest

from support import (
    OK_BARE,
    config,
    counters,
    check_response,
    d,
    ok,
    settle,
    window_start,
)

H = lambda n: (n, "HOUR")  # noqa: E731


def probe(server, domain, key="k", value="v"):
    """A check-only request (descriptor hitsAddend 0)."""
    return server.post({"domain": domain, "descriptors": [d((key, value), hitsAddend=0)]})


def wait_for(server, domain, expected, key="k", value="v", timeout=10):
    """Poll with check-only requests until the reply matches `expected`."""
    deadline = time.time() + timeout
    while True:
        reply, span = probe(server, domain, key, value)
        try:
            return check_response(reply, span, "OK", [expected])
        except AssertionError:
            if time.time() > deadline:
                raise
            time.sleep(0.3)


VALID = "- key: k\n  rate_limit: {unit: hour, requests_per_unit: 1}"

INVALID = {
    "unknown-unit": "- key: k\n  rate_limit: {unit: fortnight, requests_per_unit: 1}",
    "missing-unit": "- key: k\n  rate_limit: {requests_per_unit: 1}",
    "missing-key": "- value: v\n  rate_limit: {unit: hour, requests_per_unit: 1}",
    "unknown-field": "- key: k\n  ratelimit: {unit: hour, requests_per_unit: 1}",
    "unknown-nested-field": "- key: k\n  rate_limit: {unit: hour, requests_per_unit: 1, burst: 3}",
    "unlimited-with-unit": "- key: k\n  rate_limit: {unlimited: true, unit: hour}",
    "duplicate-descriptor": "- key: k\n  value: v\n- key: k\n  value: v",
    "duplicate-nested-descriptor": "- key: k\n  descriptors:\n    - key: n\n    - key: n",
    "replaces-itself": "- key: k\n  rate_limit: {unit: hour, requests_per_unit: 1, name: n, replaces: [{name: n}]}",
    "replaces-empty-name": "- key: k\n  rate_limit:\n    unit: hour\n    requests_per_unit: 1\n    replaces:\n      - name:",
}


@pytest.mark.parametrize("case", [*INVALID, "malformed-yaml", "empty-domain", "duplicate-domain"])
def test_invalid_configuration_disables_limiting(valkey, launch, domain, case):
    good = domain + "g"
    files = {"config/good.yaml": config(good, VALID)}
    if case in INVALID:
        files["config/bad.yaml"] = config(domain, INVALID[case])
    elif case == "malformed-yaml":
        files["config/bad.yaml"] = f"domain: {domain}\ndescriptors: [\n"
    elif case == "empty-domain":
        files["config/bad.yaml"] = "descriptors:\n" + "\n".join("  " + line for line in VALID.splitlines()) + "\n"
    else:
        files["config/bad.yaml"] = config(good, "- key: other\n  rate_limit: {unit: hour, requests_per_unit: 2}")
    server = launch(files)
    # The service still runs and reports healthy, but no configuration is in force.
    health = server.get("/healthcheck")
    assert health.status == 200 and health.text.strip() == "OK"
    for name in (good, domain):
        reply, _ = server.post({"domain": name, "descriptors": [d(("k", "v"))]})
        assert reply.status == 400, f"{case}: expected 400 for {name}, got {reply.status} {reply.text}"
    assert counters(valkey["main"], domain) == {}


def test_every_file_in_the_tree_is_loaded(valkey, launch, domain):
    server = launch({
        "config/a.yaml": config(domain + "a", "- key: k\n  rate_limit: {unit: hour, requests_per_unit: 2}"),
        "config/b.yaml": config(domain + "b", "- key: k\n  rate_limit: {unit: hour, requests_per_unit: 3}"),
        "config/deep/c.yaml": config(domain + "c", "- key: k\n  rate_limit: {unit: hour, requests_per_unit: 4}"),
        "config/notes.txt": config(domain + "t", "- key: k\n  rate_limit: {unit: hour, requests_per_unit: 6}"),
        "outside.yaml": config(domain + "x", "- key: k\n  rate_limit: {unit: hour, requests_per_unit: 7}"),
        "other/y.yaml": config(domain + "y", "- key: k\n  rate_limit: {unit: hour, requests_per_unit: 8}"),
    })
    for suffix, expected in (("a", ok(H(2), 2)), ("b", ok(H(3), 3)), ("c", ok(H(4), 4)), ("t", ok(H(6), 6)), ("x", OK_BARE), ("y", OK_BARE)):
        reply, span = probe(server, domain + suffix)
        check_response(reply, span, "OK", [expected])


def test_empty_configuration_directory(valkey, launch, runtime, domain):
    runtime.mkdir("config")
    server = launch()
    reply, span = server.post({"domain": domain, "descriptors": [d(("k", "v")), d(("x", "y"))]})
    check_response(reply, span, "OK", [OK_BARE, OK_BARE])
    assert counters(valkey["main"], domain) == {}


def test_merge_domain_config(valkey, launch, domain):
    files = {
        "config/one.yaml": config(domain, "- key: k1\n  rate_limit: {unit: hour, requests_per_unit: 1}"),
        "config/two.yaml": config(domain, "- key: k2\n  rate_limit: {unit: hour, requests_per_unit: 2}"),
    }
    separate = launch(files)
    reply, _ = separate.post({"domain": domain, "descriptors": [d(("k1", "v"), hitsAddend=0)]})
    assert reply.status == 400, reply.text
    merged = launch(MERGE_DOMAIN_CONFIG="true")
    reply, span = merged.post({"domain": domain, "descriptors": [d(("k1", "v"), hitsAddend=0), d(("k2", "v"), hitsAddend=0)]})
    check_response(reply, span, "OK", [ok(H(1), 1), ok(H(2), 2)])


def test_merge_still_rejects_duplicate_descriptors(valkey, launch, domain):
    server = launch({
        "config/one.yaml": config(domain, "- key: k1\n  rate_limit: {unit: hour, requests_per_unit: 1}"),
        "config/two.yaml": config(domain, "- key: k1\n  rate_limit: {unit: hour, requests_per_unit: 2}"),
    }, MERGE_DOMAIN_CONFIG="true")
    reply, _ = server.post({"domain": domain, "descriptors": [d(("k1", "v"), hitsAddend=0)]})
    assert reply.status == 400, reply.text
    assert counters(valkey["main"], domain) == {}


def test_reload_recovers_from_invalid_startup_config(valkey, launch, runtime, domain):
    server = launch({"config/limits.yaml": config(domain, "- key: k\n  rate_limit: {unit: fortnight, requests_per_unit: 1}")})
    reply, _ = probe(server, domain)
    assert reply.status == 400, reply.text
    runtime.write("config/limits.yaml", config(domain, "- key: k\n  rate_limit: {unit: hour, requests_per_unit: 4}"))
    wait_for(server, domain, ok(H(4), 4))


@pytest.mark.parametrize("watch_root", ["false", "true"])
def test_runtime_directory_settings(valkey, launch, domain, watch_root):
    server = launch({
        "s1/limits/x.yaml": config(domain, "- key: k\n  rate_limit: {unit: hour, requests_per_unit: 11}"),
        "s1/stray.yaml": config(domain + "o", "- key: k\n  rate_limit: {unit: hour, requests_per_unit: 12}"),
        "s1/config/y.yaml": config(domain + "c", "- key: k\n  rate_limit: {unit: hour, requests_per_unit: 13}"),
        "limits/z.yaml": config(domain + "z", "- key: k\n  rate_limit: {unit: hour, requests_per_unit: 14}"),
    }, RUNTIME_SUBDIRECTORY="s1", RUNTIME_APPDIRECTORY="limits", RUNTIME_WATCH_ROOT=watch_root)
    for name, expected in ((domain, ok(H(11), 11)), (domain + "o", OK_BARE), (domain + "c", OK_BARE), (domain + "z", OK_BARE)):
        reply, span = probe(server, name)
        check_response(reply, span, "OK", [expected])


def test_reload_on_file_changes(valkey, launch, runtime, domain):
    other = domain + "o"
    server = launch({"config/limits.yaml": config(domain, "- key: k\n  rate_limit: {unit: hour, requests_per_unit: 5}")})
    settle("HOUR", margin=40)
    key = f"{domain}_k_v_{window_start('HOUR')}"
    reply, span = server.post({"domain": domain, "descriptors": [d(("k", "v"))]})
    check_response(reply, span, "OK", [ok(H(5), 4)])

    runtime.write("config/limits.yaml", config(domain, "- key: k\n  rate_limit: {unit: hour, requests_per_unit: 50}"))
    wait_for(server, domain, ok(H(50), 49))
    runtime.write("config/other.yaml", config(other, "- key: k\n  rate_limit: {unit: hour, requests_per_unit: 7}"))
    wait_for(server, other, ok(H(7), 7))

    # An invalid edit is ignored: the last good configuration stays in force.
    runtime.write("config/limits.yaml", config(domain, "- key: k\n  bogus: 1"))
    time.sleep(3)
    for name, expected in ((domain, ok(H(50), 49)), (other, ok(H(7), 7))):
        reply, span = probe(server, name)
        check_response(reply, span, "OK", [expected])

    runtime.write("config/limits.yaml", config(domain, "- key: k\n  rate_limit: {unit: hour, requests_per_unit: 9}"))
    wait_for(server, domain, ok(H(9), 8))
    runtime.remove("config/other.yaml")
    wait_for(server, other, OK_BARE)
    assert counters(valkey["main"], domain) == {key: 1, f"{other}_k_v_{window_start('HOUR')}": 0}


def test_watch_root_reloads_on_symlink_swap(valkey, launch, runtime, domain):
    b, c = domain + "b", domain + "c"
    runtime.write("v1/rl/config/x.yaml", config(domain, "- key: k\n  rate_limit: {unit: hour, requests_per_unit: 21}"))
    runtime.write("v2/rl/config/x.yaml", config(domain, "- key: k\n  rate_limit: {unit: hour, requests_per_unit: 22}"))
    runtime.write("v2/rl/config/y.yaml", config(b, "- key: k\n  rate_limit: {unit: hour, requests_per_unit: 5}"))
    runtime.symlink("v1", "current")
    server = launch(RUNTIME_ROOT=str(runtime.root / "current"), RUNTIME_SUBDIRECTORY="rl", RUNTIME_WATCH_ROOT=None)
    reply, span = probe(server, domain)
    check_response(reply, span, "OK", [ok(H(21), 21)])

    # In this mode, edits inside the current tree are not picked up...
    runtime.write("v1/rl/config/x.yaml", config(domain, "- key: k\n  rate_limit: {unit: hour, requests_per_unit: 31}"))
    runtime.write("v1/rl/config/z.yaml", config(c, "- key: k\n  rate_limit: {unit: hour, requests_per_unit: 3}"))
    time.sleep(3)
    for name, expected in ((domain, ok(H(21), 21)), (c, OK_BARE)):
        reply, span = probe(server, name)
        check_response(reply, span, "OK", [expected])

    # ...but replacing the root symlink reloads everything beneath it.
    runtime.symlink("v2", "current")
    wait_for(server, domain, ok(H(22), 22))
    for name, expected in ((b, ok(H(5), 5)), (c, OK_BARE)):
        reply, span = probe(server, name)
        check_response(reply, span, "OK", [expected])
    runtime.symlink("v1", "current")
    wait_for(server, domain, ok(H(31), 31))
    for name, expected in ((b, OK_BARE), (c, ok(H(3), 3))):
        reply, span = probe(server, name)
        check_response(reply, span, "OK", [expected])
