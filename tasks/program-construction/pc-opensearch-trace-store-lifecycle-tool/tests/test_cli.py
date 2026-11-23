"""Invocation level: usage, argument validation, environment/flag precedence and prefix naming."""

import time

import pytest

from harness import run_program
from support import FAMILIES, failed, family_layout, ok, rollover_tool


def output(result):
    return result.stdout + result.stderr


def test_help_lists_actions_and_flags():
    top = ok(run_program(["es-rollover", "--help"]))
    for word in ("init", "rollover", "lookback", "--index-prefix", "--skip-dependencies", "--adaptive-sampling"):
        assert word in output(top), word
    init = ok(run_program(["es-rollover", "init", "--help"]))
    for word in ("--shards", "--replicas", "--span-numeric-attributes", "--span-total-fields-limit"):
        assert word in output(init), word
    assert "--conditions" in output(ok(run_program(["es-rollover", "rollover", "--help"])))
    lookback = output(ok(run_program(["es-rollover", "lookback", "--help"])))
    assert "--unit" in lookback and "--unit-count" in lookback


@pytest.mark.parametrize("args", [
    ["init"], ["rollover"], ["lookback"],
    ["init", "http://127.0.0.1:9200", "http://127.0.0.1:9200"],
    ["bogus", "http://127.0.0.1:9200"],
])
def test_wrong_arguments_fail(args):
    failed(run_program(["es-rollover", *args]))


@pytest.mark.parametrize("url", ["http://127.0.0.1:1", "not-a-url"])
def test_unusable_cluster_url_fails(url):
    failed(run_program(["es-rollover", "init", url, "--index-prefix", "unused"]))


@pytest.mark.timeout(600)
def test_environment_supplies_flags(cluster, prefix):
    env = {"INDEX_PREFIX": prefix, "SHARDS": "2", "REPLICAS": "0", "SKIP_DEPENDENCIES": "true"}
    ok(run_program(["es-rollover", "init", cluster.url], env=env))
    p = prefix + "-"
    assert cluster.indices(p) == family_layout(p, [1], families=("span", "service"))
    for family in ("span", "service"):
        settings = cluster.settings(f"{p}jaeger-{family}-000001")
        assert (settings["index.number_of_shards"], settings["index.number_of_replicas"]) == ("2", "0")
    assert sorted(cluster.templates(p)) == [f"{p}jaeger-service", f"{p}jaeger-span"]

    ok(run_program(["es-rollover", "rollover", cluster.url], env={"INDEX_PREFIX": prefix, "CONDITIONS": "{}", "SKIP_DEPENDENCIES": "true"}))
    assert cluster.indices(p) == family_layout(p, [1, 2], families=("span", "service"))


def test_flags_override_environment(cluster, prefix):
    other = prefix + "env"
    env = {"INDEX_PREFIX": other, "SHARDS": "3", "REPLICAS": "1"}
    ok(run_program(["es-rollover", "init", cluster.url, "--index-prefix", prefix, "--replicas", "0"], env=env))
    p = prefix + "-"
    assert cluster.indices(p) == family_layout(p, [1])
    for family in FAMILIES:
        settings = cluster.settings(f"{p}jaeger-{family}-000001")
        assert (settings["index.number_of_shards"], settings["index.number_of_replicas"]) == ("3", "0")
    assert cluster.indices(other + "-") == {}
    assert cluster.templates(other + "-") == {}


def test_lookback_environment_and_precedence(cluster, prefix):
    p = prefix + "-"
    ok(rollover_tool("init", cluster.url, "--index-prefix", prefix, "--shards", "1", "--replicas", "0"))
    ok(rollover_tool("rollover", cluster.url, "--index-prefix", prefix, "--conditions", "{}"))
    before = cluster.indices(p)
    # --unit wins over UNIT, but UNIT_COUNT still applies and is invalid.
    failed(rollover_tool("lookback", cluster.url, "--index-prefix", prefix, "--unit", "seconds", env={"UNIT": "fortnights", "UNIT_COUNT": "0"}))
    failed(rollover_tool("lookback", cluster.url, "--index-prefix", prefix, env={"UNIT": "fortnights"}))
    assert cluster.indices(p) == before
    time.sleep(2.2)
    ok(rollover_tool("lookback", cluster.url, env={"INDEX_PREFIX": prefix, "UNIT": "seconds", "UNIT_COUNT": "1"}))
    assert cluster.indices(p) == family_layout(p, [1, 2], read=[2])


def test_prefix_with_trailing_dash_is_not_doubled(cluster, prefix):
    p = prefix + "-"
    ok(rollover_tool("init", cluster.url, "--index-prefix", p, "--shards", "1", "--replicas", "0"))
    assert cluster.indices(p) == family_layout(p, [1])
    assert sorted(cluster.templates(p)) == sorted(f"{p}jaeger-{f}" for f in FAMILIES)
    assert cluster.indices(p + "-") == {}


def test_empty_prefix_names(cluster):
    try:
        ok(rollover_tool("init", cluster.url, "--shards", "1", "--replicas", "0"))
        indices = {k: v for k, v in cluster.indices("").items()}
        assert indices == family_layout("", [1])
        templates = cluster.templates("")
        assert sorted(templates) == sorted(f"jaeger-{f}" for f in FAMILIES)
        assert templates["jaeger-span"]["index_patterns"] == ["*jaeger-span-*"]
    finally:
        cluster.request("DELETE", "/jaeger-*?expand_wildcards=all")
        for family in FAMILIES:
            cluster.request("DELETE", f"/_template/jaeger-{family}")


@pytest.mark.parametrize("args", [["--shards", "abc"], ["--shards", "0"], ["--replicas", "x"]])
def test_init_rejects_unusable_index_options(cluster, prefix, args):
    failed(rollover_tool("init", cluster.url, "--index-prefix", prefix, *args))
    assert cluster.indices(prefix + "-") == {}
    assert cluster.templates(prefix + "-") == {}


def test_init_rejects_uppercase_prefix(cluster):
    failed(rollover_tool("init", cluster.url, "--index-prefix", "UPPER", "--shards", "1", "--replicas", "0"))
    assert cluster.templates("UPPER-") == {}
