"""Condition semantics: quantifiers, operators and how extracted values are matched."""

import json

import pytest

from alertkit import check_state_doc, cleaned, file_rule, output_dir, read_lines, run_once, tag

S = "hits.hits._source."

# Upstream config/conditions_test.go tables, rebuilt as real documents.
DOCS = [
    {"n": 10, "s": "alpha", "f": 2.5, "b": True, "arr": [1, 5], "nul": None, "obj": {"x": 1}},
    {"n": 20, "s": "beta", "f": 3.0, "b": False, "arr": [7], "obj": {"x": 2}},
    {"n": 30, "s": "gamma", "f": 2.5, "b": True, "arr": []},
]
PROPERTIES = {"n": {"type": "long"}, "s": {"type": "keyword"}, "f": {"type": "float"}, "b": {"type": "boolean"}, "arr": {"type": "long"}}
BODY = {"query": {"match_all": {}}, "sort": [{"n": "asc"}], "aggs": {"by_s": {"terms": {"field": "s", "order": {"_key": "asc"}}}}}


@pytest.fixture
def values(cluster, indices):
    name = "cond-" + tag()
    indices.append(name)
    cluster.create_index(name, PROPERTIES, DOCS)
    return name


def fired_rules(cluster, root, index, daemon, cases):
    """Run one rule per case; return {case: number of alerts written}."""
    out = output_dir(root)
    suffix = tag()
    names = {}
    for case, conditions in cases.items():
        name = f"Cond {case} {suffix}"
        names[case] = cleaned(name)
        text = json.dumps(file_rule(name, index, BODY, out, f"{case}.jsonl", conditions=conditions))
        # Literal number spellings that Python would not emit itself.
        daemon.add_rule(f"{case}.json", text.replace('"@1e1@"', "1e1"))
    run_once(daemon, cluster, list(names.values()))
    for case, rule_name in names.items():
        (doc,) = cluster.state_docs(rule_name)
        lines = read_lines(out / f"{case}.jsonl")
        assert len(lines) <= 1, (case, lines)
        if not lines:
            # A suppressed execution is recorded without hits.
            check_state_doc(doc, rule_name, [])
        else:
            assert doc["hits_count"] == 3, (case, doc)
    return {case for case in cases if read_lines(out / f"{case}.jsonl")}


def test_numeric_operators_and_quantifiers(cluster, root, values, daemon):
    cases = {
        "any-gt-hit": [{"field": S + "n", "quantifier": "any", "gt": 25}],
        "any-gt-boundary": [{"field": S + "n", "quantifier": "any", "gt": 30}],
        "all-ge-boundary": [{"field": S + "n", "quantifier": "all", "ge": 10}],
        "all-gt-boundary": [{"field": S + "n", "quantifier": "all", "gt": 10}],
        "none-gt-boundary": [{"field": S + "n", "quantifier": "none", "gt": 30}],
        "none-ge-boundary": [{"field": S + "n", "quantifier": "none", "ge": 30}],
        "default-lt-boundary": [{"field": S + "n", "lt": 10}],
        "default-le-boundary": [{"field": S + "n", "le": 10}],
        "eq-integer": [{"field": S + "n", "eq": 20}],
        "eq-decimal-spelling": [{"field": S + "n", "eq": 20.0}],
        "eq-exponent-spelling": [{"field": S + "n", "eq": "@1e1@"}],
        "eq-fraction": [{"field": S + "f", "eq": 2.5}],
        "all-gt-fraction": [{"field": S + "f", "quantifier": "all", "gt": 2.5}],
        "all-ne-present": [{"field": S + "n", "quantifier": "all", "ne": 10}],
        "all-ne-absent": [{"field": S + "n", "quantifier": "all", "ne": 15}],
        "range-inside": [{"field": S + "n", "gt": 15, "lt": 25}],
        "range-empty": [{"field": S + "n", "gt": 20, "lt": 30}],
        "ge-and-ne": [{"field": S + "n", "ge": 30, "ne": 30}],
    }
    assert fired_rules(cluster, root, values, daemon, cases) == {
        "any-gt-hit",
        "all-ge-boundary",
        "none-gt-boundary",
        "default-le-boundary",
        "eq-integer",
        "eq-decimal-spelling",
        "eq-exponent-spelling",
        "eq-fraction",
        "all-ne-absent",
        "range-inside",
    }


def test_string_operators(cluster, root, values, daemon):
    cases = {
        "eq-present": [{"field": S + "s", "eq": "beta"}],
        "eq-absent": [{"field": S + "s", "eq": "delta"}],
        "eq-case": [{"field": S + "s", "eq": "Beta"}],
        "all-ne-present": [{"field": S + "s", "quantifier": "all", "ne": "alpha"}],
        "all-ne-absent": [{"field": S + "s", "quantifier": "all", "ne": "delta"}],
        "none-eq-present": [{"field": S + "s", "quantifier": "none", "eq": "gamma"}],
        "bucket-key": [{"field": "aggregations.by_s.buckets.key", "quantifier": "all", "ne": "zeta"}],
    }
    assert fired_rules(cluster, root, values, daemon, cases) == {"eq-present", "all-ne-absent", "bucket-key"}


def test_operators_of_another_type_do_not_restrict(cluster, root, values, daemon):
    cases = {
        "number-op-on-strings": [{"field": S + "s", "gt": 5}],
        "all-number-op-on-strings": [{"field": S + "s", "quantifier": "all", "eq": 3}],
        "none-number-op-on-strings": [{"field": S + "s", "quantifier": "none", "lt": 0}],
        "string-op-on-numbers": [{"field": S + "n", "quantifier": "all", "eq": "10"}],
        "string-spelled-number": [{"field": S + "f", "eq": "2.5"}],
        "number-op-on-booleans": [{"field": S + "b", "eq": 5}],
        "none-on-booleans": [{"field": S + "b", "quantifier": "none", "gt": 0}],
        "none-string-on-boolean": [{"field": "timed_out", "quantifier": "none", "eq": "x"}],
    }
    assert fired_rules(cluster, root, values, daemon, cases) == {
        "number-op-on-strings",
        "all-number-op-on-strings",
        "string-op-on-numbers",
        "string-spelled-number",
        "number-op-on-booleans",
    }


def test_arrays_nulls_objects_and_missing_paths(cluster, root, values, daemon):
    cases = {
        "array-any": [{"field": S + "arr", "gt": 6}],
        "array-all-lt": [{"field": S + "arr", "quantifier": "all", "lt": 6}],
        "array-all-ge": [{"field": S + "arr", "quantifier": "all", "ge": 1}],
        "missing-in-hits-any": [{"field": S + "nope", "eq": 1}],
        "missing-in-hits-all": [{"field": S + "nope", "quantifier": "all", "eq": 1}],
        "missing-in-hits-none": [{"field": S + "nope", "quantifier": "none", "eq": 1}],
        "missing-object-any": [{"field": "nope.deeper", "gt": 1}],
        "missing-object-none": [{"field": "nope.deeper", "quantifier": "none", "gt": 1}],
        "null-any": [{"field": S + "nul", "gt": 100}],
        "null-all": [{"field": S + "nul", "quantifier": "all", "gt": 100}],
        "objects-any": [{"field": S + "obj", "eq": 1}],
        "objects-none": [{"field": S + "obj", "quantifier": "none", "eq": 1}],
        "hits-none": [{"field": "hits.hits", "quantifier": "none", "eq": 1}],
    }
    assert fired_rules(cluster, root, values, daemon, cases) == {
        "array-any",
        "array-all-ge",
        "missing-in-hits-all",
        "missing-in-hits-none",
        "missing-object-any",
        "null-all",
        "objects-any",
    }


def test_response_metadata_aggregations_and_conjunction(cluster, root, values, daemon):
    cases = {
        "total-eq": [{"field": "hits.total.value", "eq": 3}],
        "total-gt": [{"field": "hits.total.value", "gt": 3}],
        "took-ge": [{"field": "took", "ge": 0}],
        "bucket-counts-all": [{"field": "aggregations.by_s.buckets.doc_count", "quantifier": "all", "eq": 1}],
        "bucket-key-any": [{"field": "aggregations.by_s.buckets.key", "eq": "gamma"}],
        "both-hold": [{"field": S + "n", "gt": 25}, {"field": S + "s", "eq": "alpha"}],
        "second-fails": [{"field": S + "n", "gt": 25}, {"field": S + "s", "eq": "delta"}],
        "first-fails": [{"field": S + "n", "gt": 99}, {"field": S + "s", "eq": "alpha"}],
    }
    assert fired_rules(cluster, root, values, daemon, cases) == {"total-eq", "took-ge", "bucket-counts-all", "bucket-key-any", "both-hold"}


def test_suppressed_rule_sends_nothing_anywhere(cluster, root, values, daemon, topic):
    out = output_dir(root)
    suffix = tag()
    rules = {}
    for key, threshold in (("loud", 25), ("quiet", 30)):
        rule = file_rule(f"Threshold {key} {suffix}", values, BODY, out, f"{key}.jsonl", conditions=[{"field": S + "n", "gt": threshold}])
        rule["outputs"].append({"type": "sns", "config": {"region": "us-east-1", "topic_arn": topic.arn, "template": "{{range .}}{{.Filter}};{{end}}"}})
        rules[key] = rule
        daemon.add_rule(f"{key}.json", rule)
    run_once(daemon, cluster, [cleaned(rule["name"]) for rule in rules.values()])

    assert topic.messages(1) == [f"[{rules['loud']['name']}]\nhits.hits._source;"]
    assert len(read_lines(out / "loud.jsonl")) == 1
    assert not (out / "quiet.jsonl").exists()
    (quiet,) = cluster.state_docs(cleaned(rules["quiet"]["name"]))
    check_state_doc(quiet, cleaned(rules["quiet"]["name"]), [])
    (loud,) = cluster.state_docs(cleaned(rules["loud"]["name"]))
    check_state_doc(loud, cleaned(rules["loud"]["name"]), [{k: v for k, v in doc.items()} for doc in DOCS])
