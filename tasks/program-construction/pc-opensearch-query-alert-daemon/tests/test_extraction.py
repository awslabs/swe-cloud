"""What one execution extracts from a search response: filter records, body text and state hits."""

import json

from alertkit import body_text, check_state_doc, cleaned, file_rule, instant, now, output_dir, read_lines, run_once, same, tag

ERRORS_BY_LATENCY = {
    "query": {"term": {"level": "error"}},
    "size": 10,
    "sort": [{"latency": "desc"}],
    "_source": ["service", "latency", "msg", "meta", "tags"],
}

# Native renderings of the three error documents' selected _source objects.
TEXT_BILLING = "\n".join(['{', '    "latency": 300,', '    "msg": "billing failure",', '    "service": "billing",', '    "tags": []', '}'])
TEXT_API_ONE = "\n".join(
    ['{', '    "latency": 120,', '    "meta": {', '        "n": 1,', '        "zone": "z1"', '    },', '    "msg": "api timeout one",',
     '    "service": "api",', '    "tags": [', '        "a",', '        "b"', '    ]', '}']
)
TEXT_API_TWO = "\n".join(
    ['{', '    "latency": 80,', '    "meta": {', '        "zone": "z2"', '    },', '    "msg": "api timeout two",', '    "service": "api",',
     '    "tags": [', '        "c"', '    ]', '}']
)
SOURCE_BILLING = {"latency": 300, "msg": "billing failure", "service": "billing", "tags": []}
SOURCE_API_ONE = {"latency": 120, "meta": {"n": 1, "zone": "z1"}, "msg": "api timeout one", "service": "api", "tags": ["a", "b"]}
SOURCE_API_TWO = {"latency": 80, "meta": {"zone": "z2"}, "msg": "api timeout two", "service": "api", "tags": ["c"]}


def single_line(path):
    lines = read_lines(path)
    assert len(lines) == 1, lines
    return lines[0]


def test_filter_and_body_records(cluster, root, logs, daemon):
    out = output_dir(root)
    name = "Error Summary " + tag()
    body = {**ERRORS_BY_LATENCY, "aggs": {"svc": {"terms": {"field": "service", "order": {"_key": "asc"}}, "aggs": {"lvl": {"terms": {"field": "level"}}}}}}
    filters = ["aggregations.svc.buckets", "aggregations.svc.buckets.lvl.buckets", "aggregations.absent.buckets"]
    daemon.add_rule("rule.json", file_rule(name, logs, body, out, "alerts.jsonl", filters=filters))
    started = now()
    run_once(daemon, cluster, [cleaned(name)])
    finished = now()

    line = single_line(out / "alerts.jsonl")
    assert set(line) == {"rule_name", "received_at", "results"}
    assert line["rule_name"] == name
    assert started <= instant(line["received_at"]) <= finished
    same(
        line["results"],
        [
            {"filter": "aggregations.svc.buckets", "fields": [{"key": "api", "doc_count": 2}, {"key": "billing", "doc_count": 1}]},
            {"filter": "aggregations.svc.buckets.lvl.buckets", "fields": [{"key": "api - error", "doc_count": 2}, {"key": "billing - error", "doc_count": 1}]},
            {"filter": "hits.hits._source", "text": body_text(TEXT_BILLING, TEXT_API_ONE, TEXT_API_TWO)},
        ],
    )
    (doc,) = cluster.state_docs(cleaned(name))
    check_state_doc(doc, cleaned(name), [SOURCE_BILLING, SOURCE_API_ONE, SOURCE_API_TWO])


def test_nested_bucket_keys_chain_through_levels(cluster, root, logs, daemon):
    out = output_dir(root)
    name = "Host Breakdown " + tag()
    ordered = {"order": {"_key": "asc"}}
    body = {
        "size": 0,
        "aggs": {"host": {"terms": {"field": "host", **ordered}, "aggs": {"svc": {"terms": {"field": "service", **ordered}, "aggs": {"lvl": {"terms": {"field": "level", **ordered}}}}}}},
    }
    filters = ["aggregations.host.buckets.svc.buckets.lvl.buckets", "aggregations.host.buckets", "aggregations.host.buckets.svc.buckets"]
    daemon.add_rule("rule.json", file_rule(name, logs, body, out, "alerts.jsonl", filters=filters))
    run_once(daemon, cluster, [cleaned(name)])

    def fields(*pairs):
        return [{"key": key, "doc_count": count} for key, count in pairs]

    # size 0: no hits, so no body record and nothing in the state document's hits.
    same(
        single_line(out / "alerts.jsonl")["results"],
        [
            {
                "filter": filters[0],
                "fields": fields(("h1 - api - error", 1), ("h1 - api - info", 1), ("h1 - billing - error", 1), ("h2 - api - error", 1), ("h3 - web - warn", 1)),
            },
            {"filter": filters[1], "fields": fields(("h1", 3), ("h2", 1), ("h3", 1))},
            {"filter": filters[2], "fields": fields(("h1 - api", 2), ("h1 - billing", 1), ("h2 - api", 1), ("h3 - web", 1))},
        ],
    )
    (doc,) = cluster.state_docs(cleaned(name))
    check_state_doc(doc, cleaned(name), [])


def test_which_buckets_become_fields(cluster, root, logs, daemon):
    out = output_dir(root)
    name = "Bucket Kinds " + tag()
    ordered = {"order": {"_key": "asc"}}
    body = {
        "query": {"term": {"level": "error"}},
        "size": 0,
        "aggs": {
            "by_latency": {"terms": {"field": "latency", **ordered}},
            "by_flag": {"terms": {"field": "ok", **ordered}},
            "with_empty": {"terms": {"field": "service", "min_doc_count": 0, **ordered}},
            "bands": {"range": {"field": "latency", "ranges": [{"to": 100}, {"from": 100, "to": 200}, {"from": 1000}]}},
            "histogram": {"histogram": {"field": "latency", "interval": 100, "min_doc_count": 0}},
            "anonymous": {"filters": {"filters": [{"term": {"service": "api"}}, {"term": {"service": "billing"}}]}},
            "named": {"filters": {"filters": {"api": {"term": {"service": "api"}}}}},
        },
    }
    filters = [f"aggregations.{agg}.buckets" for agg in ("by_latency", "by_flag", "with_empty", "bands", "histogram", "anonymous", "named")]
    daemon.add_rule("rule.json", file_rule(name, logs, body, out, "alerts.jsonl", filters=filters))
    run_once(daemon, cluster, [cleaned(name)])

    def fields(*pairs):
        return [{"key": key, "doc_count": count} for key, count in pairs]

    same(
        single_line(out / "alerts.jsonl")["results"],
        [
            {"filter": filters[0], "fields": fields(("80", 1), ("120", 1), ("300", 1))},
            {"filter": filters[1], "fields": fields(("0", 2), ("1", 1))},
            {"filter": filters[2], "fields": fields(("api", 2), ("billing", 1))},
            {"filter": filters[3], "fields": fields(("*-100.0", 1), ("100.0-200.0", 1))},
            {"filter": filters[4], "fields": fields(("0.0", 1), ("100.0", 1), ("300.0", 1))},
        ],
    )


def test_body_field_variants(cluster, root, logs, daemon):
    out = output_dir(root)
    suffix = tag()
    ordered = {"order": {"_key": "asc"}}
    rules = {
        "whole-hits": file_rule(
            "Whole Hits " + suffix, logs, {"query": {"term": {"level": "warn"}}, "sort": [{"latency": "asc"}], "_source": ["msg"]}, out, "whole.jsonl", body_field="hits.hits"
        ),
        "ids-only": file_rule("Ids Only " + suffix, logs, ERRORS_BY_LATENCY, out, "ids.jsonl", body_field="hits.hits._id"),
        "nested-objects": file_rule("Nested Objects " + suffix, logs, ERRORS_BY_LATENCY, out, "meta.jsonl", body_field="hits.hits._source.meta"),
        "top-hits": file_rule(
            "Top Hits " + suffix,
            logs,
            {"size": 0, "aggs": {"svc": {"terms": {"field": "service", **ordered}, "aggs": {"top": {"top_hits": {"size": 1, "sort": [{"latency": "desc"}], "_source": ["latency", "level"]}}}}}},
            out,
            "top.jsonl",
            body_field="aggregations.svc.buckets.top.hits.hits._source",
        ),
    }
    for key, rule in rules.items():
        daemon.add_rule(f"{key}.json", rule)
    run_once(daemon, cluster, [cleaned(rule["name"]) for rule in rules.values()])

    whole_hit = {"_id": "4", "_index": logs, "_score": None, "_source": {"msg": "slow"}, "sort": [40]}
    text_whole = "\n".join(
        ['{', '    "_id": "4",', f'    "_index": "{logs}",', '    "_score": null,', '    "_source": {', '        "msg": "slow"', '    },', '    "sort": [', '        40', '    ]', '}']
    )
    same(single_line(out / "whole.jsonl")["results"], [{"filter": "hits.hits", "text": text_whole}])
    check_state_doc(cluster.state_docs(cleaned(rules["whole-hits"]["name"]))[0], cleaned(rules["whole-hits"]["name"]), [whole_hit])

    # Matches that are not objects produce neither a record nor state hits, so nothing is sent.
    assert not (out / "ids.jsonl").exists()
    check_state_doc(cluster.state_docs(cleaned(rules["ids-only"]["name"]))[0], cleaned(rules["ids-only"]["name"]), [])

    text_meta = body_text(
        "\n".join(['{', '    "n": 1,', '    "zone": "z1"', '}']),
        "\n".join(['{', '    "zone": "z2"', '}']),
    )
    same(single_line(out / "meta.jsonl")["results"], [{"filter": "hits.hits._source.meta", "text": text_meta}])
    check_state_doc(cluster.state_docs(cleaned(rules["nested-objects"]["name"]))[0], cleaned(rules["nested-objects"]["name"]), [{"n": 1, "zone": "z1"}, {"zone": "z2"}])

    top = [{"latency": 120, "level": "error"}, {"latency": 300, "level": "error"}, {"latency": 40, "level": "warn"}]
    text_top = body_text(*("\n".join(['{', f'    "latency": {doc["latency"]},', f'    "level": "{doc["level"]}"', '}']) for doc in top))
    same(single_line(out / "top.jsonl")["results"], [{"filter": "aggregations.svc.buckets.top.hits.hits._source", "text": text_top}])
    check_state_doc(cluster.state_docs(cleaned(rules["top-hits"]["name"]))[0], cleaned(rules["top-hits"]["name"]), top)


def test_body_given_as_json_string(cluster, root, logs, daemon):
    out = output_dir(root)
    name = "String Body " + tag()
    body = {**ERRORS_BY_LATENCY, "aggs": {"svc": {"terms": {"field": "service", "order": {"_key": "desc"}}}}}
    daemon.add_rule("rule.json", file_rule(name, logs, json.dumps(body), out, "alerts.jsonl", filters=["aggregations.svc.buckets"]))
    run_once(daemon, cluster, [cleaned(name)])
    same(
        single_line(out / "alerts.jsonl")["results"],
        [
            {"filter": "aggregations.svc.buckets", "fields": [{"key": "billing", "doc_count": 1}, {"key": "api", "doc_count": 2}]},
            {"filter": "hits.hits._source", "text": body_text(TEXT_BILLING, TEXT_API_ONE, TEXT_API_TWO)},
        ],
    )


def test_empty_results_record_state_only(cluster, root, logs, daemon):
    out = output_dir(root)
    suffix = tag()
    nothing = {"query": {"term": {"level": "fatal"}}, "aggs": {"svc": {"terms": {"field": "service"}}}}
    rules = [
        file_rule("No Matches " + suffix, logs, nothing, out, "none.jsonl", filters=["aggregations.svc.buckets"]),
        file_rule("Missing Index " + suffix, "absent-" + suffix, {"query": {"match_all": {}}}, out, "missing.jsonl"),
        file_rule("Healthy Neighbour " + suffix, logs, {"query": {"term": {"level": "info"}}, "_source": ["msg"]}, out, "info.jsonl"),
    ]
    for number, rule in enumerate(rules):
        daemon.add_rule(f"{number}.json", rule)
    run_once(daemon, cluster, [cleaned(rule["name"]) for rule in rules])

    # No results and a failed query both send nothing, yet each execution is still recorded.
    assert not (out / "none.jsonl").exists()
    assert not (out / "missing.jsonl").exists()
    for rule in rules[:2]:
        (doc,) = cluster.state_docs(cleaned(rule["name"]))
        check_state_doc(doc, cleaned(rule["name"]), [])
    same(single_line(out / "info.jsonl")["results"], [{"filter": "hits.hits._source", "text": '{\n    "msg": "healthy"\n}'}])


def test_numeric_bucket_keys_never_join_the_chain(cluster, root, logs, daemon):
    out = output_dir(root)
    suffix = tag()
    ordered = {"order": {"_key": "asc"}}
    histogram = {"histogram": {"field": "latency", "interval": 100}}
    rules = {
        "leaf": (
            {"size": 0, "aggs": {"svc": {"terms": {"field": "service", **ordered}, "aggs": {"lat": {"terms": {"field": "latency", **ordered}}}}}},
            ["aggregations.svc.buckets.lat.buckets"],
        ),
        "parent": (
            {"size": 0, "aggs": {"hist": {**histogram, "aggs": {"svc": {"terms": {"field": "service", **ordered}}}}}},
            ["aggregations.hist.buckets.svc.buckets"],
        ),
        "middle": (
            {"size": 0, "aggs": {"svc": {"terms": {"field": "service", **ordered}, "aggs": {"hist": {**histogram, "aggs": {"lvl": {"terms": {"field": "level", **ordered}}}}}}}},
            ["aggregations.svc.buckets.hist.buckets.lvl.buckets", "aggregations.svc.buckets.hist.buckets"],
        ),
    }
    for key, (body, filters) in rules.items():
        daemon.add_rule(f"{key}.json", file_rule(f"Numeric {key} {suffix}", logs, body, out, f"{key}.jsonl", filters=filters))
    run_once(daemon, cluster, [cleaned(f"Numeric {key} {suffix}") for key in rules])

    def fields(*pairs):
        return [{"key": key, "doc_count": count} for key, count in pairs]

    # A numeric leaf key is not prefixed; a numeric parent adds nothing to its children's keys.
    # Equal keys from different parents stay separate fields.
    same(single_line(out / "leaf.jsonl")["results"], [{"filter": rules["leaf"][1][0], "fields": fields(("5", 1), ("80", 1), ("120", 1), ("300", 1), ("40", 1))}])
    same(single_line(out / "parent.jsonl")["results"], [{"filter": rules["parent"][1][0], "fields": fields(("api", 2), ("web", 1), ("api", 1), ("billing", 1))}])
    same(
        single_line(out / "middle.jsonl")["results"],
        [
            {"filter": rules["middle"][1][0], "fields": fields(("api - error", 1), ("api - info", 1), ("api - error", 1), ("billing - error", 1), ("web - warn", 1))},
            {"filter": rules["middle"][1][1], "fields": fields(("0.0", 2), ("100.0", 1), ("300.0", 1), ("0.0", 1))},
        ],
    )


SPELLED_DOCS = [
    '{"level": "error", "latency": 120, "score": 2.50, "big": 1e2, "neg": -0.5, "tiny": 1.5E-3}',
    '{"level": "error", "latency": 80, "score": 100.0, "big": 10, "neg": -1, "tiny": 0.000}',
    '{"level": "error", "latency": 300, "score": 3, "big": 2E+3, "neg": 0, "tiny": 7}',
    '{"level": "info", "latency": 5, "score": 0.25, "big": 1, "neg": 1, "tiny": 1}',
]


def test_body_text_keeps_number_spellings(cluster, root, indices, daemon):
    index = "spelled-" + tag()
    indices.append(index)
    cluster.create_index(index, {"level": {"type": "keyword"}, "latency": {"type": "long"}, "score": {"type": "float"}}, SPELLED_DOCS)
    out = output_dir(root)
    suffix = tag()
    errors = {"query": {"term": {"level": "error"}}, "sort": [{"latency": "desc"}]}
    rules = {
        # Whole _source comes back as it was indexed.
        "verbatim": file_rule("Spelling Verbatim " + suffix, index, errors, out, "verbatim.jsonl"),
        # Source filtering makes the cluster re-serialize the selected values.
        "filtered": file_rule("Spelling Filtered " + suffix, index, {**errors, "_source": ["score", "big", "neg", "tiny"]}, out, "filtered.jsonl"),
    }
    for key, rule in rules.items():
        daemon.add_rule(f"{key}.json", rule)
    run_once(daemon, cluster, [cleaned(rule["name"]) for rule in rules.values()])

    def render(*members):
        return "\n".join(["{", *(f"    {member}," for member in members[:-1]), f"    {members[-1]}", "}"])

    verbatim = body_text(
        render('"big": 2E+3', '"latency": 300', '"level": "error"', '"neg": 0', '"score": 3', '"tiny": 7'),
        render('"big": 1e2', '"latency": 120', '"level": "error"', '"neg": -0.5', '"score": 2.50', '"tiny": 1.5E-3'),
        render('"big": 10', '"latency": 80', '"level": "error"', '"neg": -1', '"score": 100.0', '"tiny": 0.000'),
    )
    same(single_line(out / "verbatim.jsonl")["results"], [{"filter": "hits.hits._source", "text": verbatim}])
    filtered = body_text(
        render('"big": 2000.0', '"neg": 0', '"score": 3', '"tiny": 7'),
        render('"big": 100.0', '"neg": -0.5', '"score": 2.5', '"tiny": 0.0015'),
        render('"big": 10', '"neg": -1', '"score": 100.0', '"tiny": 0.0'),
    )
    same(single_line(out / "filtered.jsonl")["results"], [{"filter": "hits.hits._source", "text": filtered}])
    # State hits are compared by value, whatever the spelling.
    values = [
        {"big": 2000, "latency": 300, "level": "error", "neg": 0, "score": 3, "tiny": 7},
        {"big": 100, "latency": 120, "level": "error", "neg": -0.5, "score": 2.5, "tiny": 0.0015},
        {"big": 10, "latency": 80, "level": "error", "neg": -1, "score": 100, "tiny": 0},
    ]
    (doc,) = cluster.state_docs(cleaned(rules["verbatim"]["name"]))
    check_state_doc(doc, cleaned(rules["verbatim"]["name"]), values)
