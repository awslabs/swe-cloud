"""Delivery of alerts: SNS message rendering, file appends and failure handling of outputs."""

import os
import stat
import time

import pytest

from alertkit import body_text, check_state_doc, cleaned, output_dir, read_lines, run_once, same, tag, wait_lines, wait_state
from harness import give_to_candidate

# Upstream command/alert/sns and command/alert/file tests, driven through real deliveries.
BODY = {
    "query": {"match_all": {}},
    "sort": [{"n": "asc"}],
    "_source": ["n"],
    "aggs": {"svc": {"terms": {"field": "svc", "order": {"_key": "asc"}}}},
}
TEXT = body_text('{\n    "n": 1\n}', '{\n    "n": 2\n}', '{\n    "n": 3\n}')
RESULTS = [
    {"filter": "aggregations.svc.buckets", "fields": [{"key": "a", "doc_count": 2}, {"key": "b", "doc_count": 1}]},
    {"filter": "hits.hits._source", "text": TEXT},
]


@pytest.fixture
def events(cluster, indices):
    name = "events-" + tag()
    indices.append(name)
    cluster.create_index(name, {"n": {"type": "long"}, "svc": {"type": "keyword"}}, [{"n": 1, "svc": "a"}, {"n": 2, "svc": "b"}, {"n": 3, "svc": "a"}])
    return name


def sns_output(topic, template, region="us-east-1"):
    return {"type": "sns", "config": {"region": region, "topic_arn": topic.arn, "template": template}}


def file_output(path):
    return {"type": "file", "config": {"file": str(path)}}


def rule(name, index, outputs):
    return {"name": name, "index": index, "schedule": "@every 1h", "body": BODY, "filters": ["aggregations.svc.buckets"], "outputs": outputs}


def test_sns_message_renders_template_over_records(cluster, root, events, daemon, topic):
    out = output_dir(root)
    suffix = tag()
    summary = "{{range .}}{{.Filter}}:{{range .Fields}} {{.Key}}={{.Count}}{{end}}{{if .Text}} text={{len .Text}}{{end}}\n{{end}}{{ \"end\" | upper }}"
    rules = {
        "summary": rule("Summary Alert " + suffix, events, [sns_output(topic, summary), file_output(out / "summary.jsonl")]),
        "text": rule("Text Alert " + suffix, events, [sns_output(topic, "{{range .}}{{.Text}}{{end}}")]),
        "count": rule("Count Alert " + suffix, events, [sns_output(topic, "{{len .}} records, first {{(index . 0).Filter | quote}}")]),
    }
    for key, value in rules.items():
        daemon.add_rule(f"{key}.json", value)
    run_once(daemon, cluster, [cleaned(value["name"]) for value in rules.values()])

    messages = topic.messages(3)
    assert sorted(messages) == sorted(
        [
            f"[{rules['summary']['name']}]\naggregations.svc.buckets: a=2 b=1\nhits.hits._source: text={len(TEXT)}\nEND",
            f"[{rules['text']['name']}]\n{TEXT}",
            f"[{rules['count']['name']}]\n2 records, first \"aggregations.svc.buckets\"",
        ]
    )
    (line,) = read_lines(out / "summary.jsonl")
    assert line["rule_name"] == rules["summary"]["name"]
    same(line["results"], RESULTS)


def test_sns_empty_render_uses_placeholder(cluster, root, events, daemon, topic):
    suffix = tag()
    rules = {
        "empty": rule("Empty Render " + suffix, events, [sns_output(topic, "{{if false}}never{{end}}")]),
        "blank": rule("Blank Render " + suffix, events, [sns_output(topic, "{{range .}}{{end}} ")]),
    }
    for key, value in rules.items():
        daemon.add_rule(f"{key}.json", value)
    run_once(daemon, cluster, [cleaned(value["name"]) for value in rules.values()])
    assert sorted(topic.messages(2)) == sorted(
        [f"[{rules['empty']['name']}]\nNew alerts detected. See logs.", f"[{rules['blank']['name']}]\n "]
    )


def test_file_output_appends_and_is_shared(cluster, root, events, daemon):
    out = output_dir(root)
    suffix = tag()
    shared = out / "shared.jsonl"
    shared.write_text('{"previous": "entry"}\n')
    give_to_candidate(shared)
    shared.chmod(0o644)
    fresh = out / "fresh.jsonl"
    rules = [
        rule("Shared One " + suffix, events, [file_output(shared)]),
        rule("Shared Two " + suffix, events, [file_output(shared), file_output(fresh)]),
    ]
    for number, value in enumerate(rules):
        daemon.add_rule(f"{number}.json", value)
    run_once(daemon, cluster, [cleaned(value["name"]) for value in rules])

    lines = read_lines(shared)
    assert len(lines) == 3, lines
    assert lines[0] == {"previous": "entry"}
    assert sorted(line["rule_name"] for line in lines[1:]) == sorted(value["name"] for value in rules)
    for line in lines[1:]:
        assert set(line) == {"rule_name", "received_at", "results"}
        same(line["results"], RESULTS)
    (line,) = read_lines(fresh)
    assert line["rule_name"] == rules[1]["name"]
    # A file the daemon creates is private to its user.
    assert stat.S_IMODE(os.lstat(fresh).st_mode) == 0o600


def test_failed_output_does_not_stop_the_others(cluster, root, events, daemon, topic):
    out = output_dir(root)
    name = "Partial Delivery " + tag()
    daemon.add_rule(
        "rule.json",
        rule(name, events, [file_output(out / "missing-dir" / "alerts.jsonl"), sns_output(topic, "{{len .}}"), file_output(out / "ok.jsonl")]),
    )
    daemon.start()
    assert wait_state(cluster, cleaned(name))
    assert len(wait_lines(out / "ok.jsonl", 1, timeout=20)) == 1
    assert topic.messages(1, timeout=20) == [f"[{name}]\n2"]
    time.sleep(1)
    assert daemon.stop() == 0
    # The file output does not create directories.
    assert not os.path.lexists(out / "missing-dir")
    same(read_lines(out / "ok.jsonl")[0]["results"], RESULTS)
    (doc,) = cluster.state_docs(cleaned(name))
    check_state_doc(doc, cleaned(name), [{"n": 1}, {"n": 2}, {"n": 3}])


def test_failed_file_write_is_retried(cluster, root, events, daemon):
    out = output_dir(root)
    name = "Late Directory " + tag()
    late = out / "late"
    daemon.add_rule("rule.json", rule(name, events, [file_output(late / "alerts.jsonl")]))
    daemon.start()
    assert wait_state(cluster, cleaned(name))
    # The first delivery has failed by now; the directory appears before the retry.
    late.mkdir(mode=0o755)
    give_to_candidate(late)
    lines = wait_lines(late / "alerts.jsonl", 1, timeout=12)
    time.sleep(4)
    assert daemon.stop() == 0
    assert len(read_lines(late / "alerts.jsonl")) == 1, lines
    same(read_lines(late / "alerts.jsonl")[0]["results"], RESULTS)


def test_template_execution_error_publishes_nothing(cluster, root, events, daemon, topic):
    out = output_dir(root)
    name = "Broken Template " + tag()
    daemon.add_rule("rule.json", rule(name, events, [sns_output(topic, "{{.Missing}}"), file_output(out / "alerts.jsonl")]))
    daemon.start()
    assert wait_state(cluster, cleaned(name))
    assert len(wait_lines(out / "alerts.jsonl", 1, timeout=20)) == 1
    assert topic.messages(1, timeout=8) == []
    assert daemon.running()
    assert daemon.stop() == 0


def test_sns_region_is_taken_from_the_output(cluster, root, events, daemon, topic):
    # The topic exists in us-east-1 only; publishing through another region cannot reach it.
    name = "Other Region " + tag()
    daemon.add_rule("rule.json", rule(name, events, [sns_output(topic, "elsewhere", region="eu-west-1")]))
    daemon.start()
    assert wait_state(cluster, cleaned(name))
    assert topic.messages(1, timeout=8) == []
    assert daemon.stop() == 0
