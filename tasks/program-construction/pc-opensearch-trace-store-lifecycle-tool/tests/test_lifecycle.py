"""Single es-rollover actions: the indices, aliases and templates each one creates, moves or leaves alone."""

import time

import pytest

from support import (EXPECTED, FAMILIES, aging_proxy, assert_template, expected_template, failed, family_layout, lenient,
                     ok, rollover_tool)


def init(cluster, prefix, *args):
    return ok(rollover_tool("init", cluster.url, "--index-prefix", prefix, *args))


def uuids(cluster, p):
    status, body = cluster.request("GET", f"/{p}jaeger-*/_settings?flat_settings=true&expand_wildcards=all")
    return {name: info["settings"]["index.uuid"] for name, info in body.items()}


@pytest.mark.timeout(600)
def test_init_creates_first_generation(cluster, prefix):
    p = prefix + "-"
    init(cluster, prefix, "--shards", "1", "--replicas", "0")
    assert cluster.indices(p) == family_layout(p, [1])
    # Plain aliases: no write-index flag, filter or routing.
    for index, aliases in cluster.alias_details(p).items():
        assert all(body == {} for body in aliases.values()), (index, aliases)
    templates = cluster.templates(p)
    assert sorted(templates) == sorted(f"{p}jaeger-{f}" for f in FAMILIES)
    for family in FAMILIES:
        assert_template(templates[f"{p}jaeger-{family}"], expected_template(f"jaeger-{family}", p))
        settings = cluster.settings(f"{p}jaeger-{family}-000001")
        assert (settings["index.number_of_shards"], settings["index.number_of_replicas"]) == ("1", "0")
        assert settings["index.mapping.nested_fields.limit"] == "50"
    assert cluster.composable_templates(p) == []
    assert cluster.mappings(f"{p}jaeger-span-000001") == EXPECTED["span index mappings"]
    for family in ("service", "dependencies"):
        assert lenient(cluster.mappings(f"{p}jaeger-{family}-000001")) == lenient(EXPECTED[f"jaeger-{family}"]["mappings"])


def test_init_defaults_to_five_shards_one_replica(cluster, prefix):
    p = prefix + "-"
    init(cluster, prefix)
    templates = cluster.templates(p)
    for family in FAMILIES:
        assert_template(templates[f"{p}jaeger-{family}"], expected_template(f"jaeger-{family}", p, shards=5, replicas=1))
        settings = cluster.settings(f"{p}jaeger-{family}-000001")
        assert (settings["index.number_of_shards"], settings["index.number_of_replicas"]) == ("5", "1")


def test_init_is_idempotent(cluster, prefix):
    p = prefix + "-"
    init(cluster, prefix, "--shards", "1", "--replicas", "0")
    before = (cluster.indices(p), uuids(cluster, p), cluster.templates(p))
    init(cluster, prefix, "--shards", "1", "--replicas", "0")
    assert (cluster.indices(p), uuids(cluster, p), cluster.templates(p)) == before


def test_init_again_rewrites_templates_but_not_indices(cluster, prefix):
    p = prefix + "-"
    init(cluster, prefix, "--shards", "1", "--replicas", "0")
    ids = uuids(cluster, p)
    init(cluster, prefix, "--shards", "2", "--replicas", "0")
    templates = cluster.templates(p)
    for family in FAMILIES:
        assert_template(templates[f"{p}jaeger-{family}"], expected_template(f"jaeger-{family}", p, shards=2))
        assert cluster.settings(f"{p}jaeger-{family}-000001")["index.number_of_shards"] == "1"
    assert uuids(cluster, p) == ids
    assert cluster.indices(p) == family_layout(p, [1])


def test_init_family_selection(cluster, prefix):
    p = prefix + "-"
    init(cluster, prefix, "--shards", "1", "--replicas", "0", "--skip-dependencies", "--adaptive-sampling")
    families = ("span", "service", "sampling")
    assert cluster.indices(p) == family_layout(p, [1], families=families)
    templates = cluster.templates(p)
    assert sorted(templates) == sorted(f"{p}jaeger-{f}" for f in families)
    for family in families:
        assert_template(templates[f"{p}jaeger-{family}"], expected_template(f"jaeger-{family}", p))


def test_init_span_template_options(cluster, prefix):
    p = prefix + "-"
    init(cluster, prefix, "--span-total-fields-limit", "2000", "--span-numeric-attributes")
    templates = cluster.templates(p)
    assert_template(templates[f"{p}jaeger-span"], expected_template("jaeger-span numeric", p, shards=5, replicas=1))
    assert cluster.settings(f"{p}jaeger-span-000001")["index.mapping.total_fields.limit"] == "2000"
    for family in ("service", "dependencies"):
        assert_template(templates[f"{p}jaeger-{family}"], expected_template(f"jaeger-{family}", p, shards=5, replicas=1))


def test_init_priority_flags_leave_legacy_template_order(cluster, prefix):
    p = prefix + "-"
    init(cluster, prefix, "--shards", "1", "--replicas", "0", "--priority-span-template", "7", "--priority-service-template", "3")
    templates = cluster.templates(p)
    for family in FAMILIES:
        assert_template(templates[f"{p}jaeger-{family}"], expected_template(f"jaeger-{family}", p))


def test_rollover_default_condition_keeps_fresh_indices(cluster, prefix):
    p = prefix + "-"
    init(cluster, prefix, "--shards", "1", "--replicas", "0")
    before = uuids(cluster, p)
    ok(rollover_tool("rollover", cluster.url, "--index-prefix", prefix))
    assert cluster.indices(p) == family_layout(p, [1])
    assert uuids(cluster, p) == before


@pytest.mark.parametrize("conditions", ["{}", "", '{"max_docs": 0}', '{"max_age": "1ms"}'])
def test_rollover_when_conditions_hold(cluster, prefix, conditions):
    p = prefix + "-"
    init(cluster, prefix, "--shards", "1", "--replicas", "0")
    # New generations are created from the template as it stands at rollover time.
    init(cluster, prefix, "--shards", "2", "--replicas", "0")
    time.sleep(0.05)
    ok(rollover_tool("rollover", cluster.url, "--index-prefix", prefix, "--conditions", conditions))
    assert cluster.indices(p) == family_layout(p, [1, 2])
    for family in FAMILIES:
        assert cluster.settings(f"{p}jaeger-{family}-000001")["index.number_of_shards"] == "1"
        assert cluster.settings(f"{p}jaeger-{family}-000002")["index.number_of_shards"] == "2"
    for index, aliases in cluster.alias_details(p).items():
        assert all(body == {} for body in aliases.values()), (index, aliases)


def test_rollover_conditions_not_met_on_empty_indices(cluster, prefix):
    p = prefix + "-"
    init(cluster, prefix, "--shards", "1", "--replicas", "0")
    ok(rollover_tool("rollover", cluster.url, "--index-prefix", prefix, "--conditions", '{"max_docs": 1}'))
    assert cluster.indices(p) == family_layout(p, [1])


def test_repeated_rollovers_number_generations(cluster, prefix):
    p = prefix + "-"
    init(cluster, prefix, "--shards", "1", "--replicas", "0")
    for _ in range(3):
        ok(rollover_tool("rollover", cluster.url, "--index-prefix", prefix, "--conditions", "{}"))
    assert cluster.indices(p) == family_layout(p, [1, 2, 3, 4])


def test_rollover_before_init_fails_without_creating(cluster, prefix):
    p = prefix + "-"
    failed(rollover_tool("rollover", cluster.url, "--index-prefix", prefix, "--conditions", "{}"))
    assert cluster.indices(p) == {}
    assert cluster.templates(p) == {}


@pytest.mark.parametrize("conditions", ["{not json", "[1, 2]", '{"max_size": "bogus"}'])
def test_rollover_rejects_bad_conditions(cluster, prefix, conditions):
    p = prefix + "-"
    init(cluster, prefix, "--shards", "1", "--replicas", "0")
    failed(rollover_tool("rollover", cluster.url, "--index-prefix", prefix, "--conditions", conditions))
    assert cluster.indices(p) == family_layout(p, [1])


def test_rollover_family_selection_and_partial_failure(cluster, prefix):
    p = prefix + "-"
    init(cluster, prefix, "--shards", "1", "--replicas", "0", "--skip-dependencies", "--adaptive-sampling")
    # Without the flags the action expects a dependencies family: spans and services roll, then it fails.
    failed(rollover_tool("rollover", cluster.url, "--index-prefix", prefix, "--conditions", "{}"))
    assert cluster.indices(p) == family_layout(p, {"span": [1, 2], "service": [1, 2], "sampling": [1]},
                                               families=("span", "service", "sampling"))
    ok(rollover_tool("rollover", cluster.url, "--index-prefix", prefix, "--conditions", "{}",
                     "--skip-dependencies", "--adaptive-sampling"))
    assert cluster.indices(p) == family_layout(p, {"span": [1, 2, 3], "service": [1, 2, 3], "sampling": [1, 2]},
                                               families=("span", "service", "sampling"))


@pytest.mark.parametrize("unit_args", [
    ["--unit", "minutes", "--unit-count", "60"], ["--unit", "hours", "--unit-count", "1"],
    ["--unit", "days", "--unit-count", "2"], ["--unit", "weeks"], ["--unit", "months"], ["--unit", "years"],
])
def test_lookback_keeps_recent_generations(cluster, prefix, unit_args):
    p = prefix + "-"
    init(cluster, prefix, "--shards", "1", "--replicas", "0")
    ok(rollover_tool("rollover", cluster.url, "--index-prefix", prefix, "--conditions", "{}"))
    time.sleep(1.5)
    ok(rollover_tool("lookback", cluster.url, "--index-prefix", prefix, *unit_args))
    assert cluster.indices(p) == family_layout(p, [1, 2])


# Generations 1-4 are reported (through a pass-through to the cluster) as this old; generation 5 is the write index.
# Ages sit at least one unit away from each cutoff's truncation (to the minute/hour, or to midnight), so the
# outcome does not depend on the time of day the test runs.
AGED = {1: 400 * 86400, 2: 40 * 86400, 3: 3 * 86400, 4: 150 * 60}


@pytest.mark.parametrize("unit, count, detached", [
    ("minutes", "120", [1, 2, 3, 4]), ("minutes", "180", [1, 2, 3]),
    ("hours", "1", [1, 2, 3, 4]), ("hours", "3", [1, 2, 3]),
    ("days", "2", [1, 2, 3]), ("days", "5", [1, 2]),
    ("weeks", "1", [1, 2]), ("weeks", "8", [1]),
    ("months", "1", [1, 2]), ("months", "2", [1]),
    ("years", "1", [1]), ("years", "2", []),
])
def test_lookback_cutoff_per_unit(cluster, prefix, unit, count, detached):
    p = prefix + "-"
    init(cluster, prefix, "--shards", "1", "--replicas", "0")
    for _ in range(4):
        ok(rollover_tool("rollover", cluster.url, "--index-prefix", prefix, "--conditions", "{}"))
    now = int(time.time() * 1000)
    dates = {}
    for index in cluster.indices(p):
        generation = int(index[-6:])
        if generation in AGED:
            dates[index] = now - AGED[generation] * 1000
    with aging_proxy(cluster, dates) as url:
        ok(rollover_tool("lookback", url, "--index-prefix", prefix, "--unit", unit, "--unit-count", count))
    generations = [1, 2, 3, 4, 5]
    assert cluster.indices(p) == family_layout(p, generations, read=[g for g in generations if g not in detached])


def test_lookback_seconds_detaches_old_generations(cluster, prefix):
    p = prefix + "-"
    init(cluster, prefix, "--shards", "1", "--replicas", "0")
    ok(rollover_tool("rollover", cluster.url, "--index-prefix", prefix, "--conditions", "{}"))
    ok(rollover_tool("rollover", cluster.url, "--index-prefix", prefix, "--conditions", "{}"))
    ids = uuids(cluster, p)
    time.sleep(2.2)
    ok(rollover_tool("lookback", cluster.url, "--index-prefix", prefix, "--unit", "seconds", "--unit-count", "1"))
    # Indices are only detached from the read alias; none is deleted and the write index keeps both aliases.
    assert cluster.indices(p) == family_layout(p, [1, 2, 3], read=[3])
    assert uuids(cluster, p) == ids


def test_lookback_never_detaches_the_write_index(cluster, prefix):
    p = prefix + "-"
    init(cluster, prefix, "--shards", "1", "--replicas", "0")
    time.sleep(2.2)
    ok(rollover_tool("lookback", cluster.url, "--index-prefix", prefix, "--unit", "seconds", "--unit-count", "1"))
    assert cluster.indices(p) == family_layout(p, [1])


def test_lookback_family_selection(cluster, prefix):
    p = prefix + "-"
    init(cluster, prefix, "--shards", "1", "--replicas", "0")
    ok(rollover_tool("rollover", cluster.url, "--index-prefix", prefix, "--conditions", "{}"))
    time.sleep(2.2)
    ok(rollover_tool("lookback", cluster.url, "--index-prefix", prefix, "--unit", "seconds", "--skip-dependencies"))
    assert cluster.indices(p) == family_layout(p, [1, 2], read={"span": [2], "service": [2], "dependencies": [1, 2]})


def test_lookback_before_init_changes_nothing(cluster, prefix):
    p = prefix + "-"
    ok(rollover_tool("lookback", cluster.url, "--index-prefix", prefix))
    assert cluster.indices(p) == {}
    assert cluster.templates(p) == {}


@pytest.mark.parametrize("args", [["--unit", "fortnights"], ["--unit-count", "0"], ["--unit-count", "-3"], ["--unit-count", "abc"]])
def test_lookback_rejects_bad_arguments(cluster, prefix, args):
    p = prefix + "-"
    init(cluster, prefix, "--shards", "1", "--replicas", "0")
    ok(rollover_tool("rollover", cluster.url, "--index-prefix", prefix, "--conditions", "{}"))
    time.sleep(2.2)
    failed(rollover_tool("lookback", cluster.url, "--index-prefix", prefix, "--unit", "seconds", *args))
    assert cluster.indices(p) == family_layout(p, [1, 2])
