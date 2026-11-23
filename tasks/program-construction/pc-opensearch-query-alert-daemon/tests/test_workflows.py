"""End-to-end scenarios: a realistic rule set, rule-directory selection and renamed rules across restarts."""

import datetime as dt
import time

from alertkit import Daemon, body_text, check_state_doc, cleaned, file_rule, now, output_dir, read_lines, rfc3339, run_once, same, tag


def syslog_text(doc):
    syslog = doc["system"]["syslog"]
    return "\n".join(
        [
            "{",
            f'    "@timestamp": "{doc["@timestamp"]}",',
            f'    "message": "{doc["message"]}",',
            f'    "source": "{doc["source"]}",',
            '    "system": {',
            '        "syslog": {',
            f'            "hostname": "{syslog["hostname"]}",',
            '            "queue_size": {',
            f'                "value": {syslog["queue_size"]["value"]}',
            "            }",
            "        }",
            "    }",
            "}",
        ]
    )


def test_example_rule_set(cluster, root, indices, daemon, topic):
    """Adapted from the upstream examples/config/rules/test-rule.json (file output, plus filters and SNS)."""
    index = "syslog-" + tag()
    indices.append(index)
    moment = now().replace(microsecond=0)

    def entry(seconds_ago, host, queue, message, source="/var/log/system.log"):
        return {
            "@timestamp": rfc3339(moment - dt.timedelta(seconds=seconds_ago)),
            "source": source,
            "message": message,
            "system": {"syslog": {"hostname": host, "queue_size": {"value": queue}}},
        }

    docs = [
        entry(20, "host-a", 70, "queue high"),
        entry(15, "host-a", 20, "queue normal"),
        entry(10, "host-b", 30, "queue normal"),
        entry(7200, "host-c", 99, "stale entry"),
        entry(5, "host-b", 99, "other log", source="/var/log/other.log"),
    ]
    cluster.create_index(
        index,
        {"@timestamp": {"type": "date"}, "source": {"type": "keyword"}, "message": {"type": "text"},
         "system": {"properties": {"syslog": {"properties": {"hostname": {"type": "keyword"}, "queue_size": {"properties": {"value": {"type": "long"}}}}}}}},
        docs,
    )
    body = {
        "query": {
            "bool": {
                "must": [{"term": {"source": {"value": "/var/log/system.log"}}}],
                "filter": [{"range": {"@timestamp": {"gte": "now-2m/m"}}}],
            }
        },
        "aggregations": {"hostname": {"terms": {"field": "system.syslog.hostname", "min_doc_count": 1}}},
        "size": 20,
        "sort": [{"@timestamp": "desc"}],
    }
    out = output_dir(root)
    suffix = tag()
    rules = {}
    for key, threshold in (("busy", 50), ("overloaded", 80)):
        rule = file_rule(
            f"Example Error {key} {suffix}",
            index,
            body,
            out,
            f"{key}.jsonl",
            schedule="@every 2m",
            filters=["aggregations.hostname.buckets"],
            conditions=[{"field": "hits.hits._source.system.syslog.queue_size.value", "quantifier": "any", "gt": threshold}],
        )
        rule["outputs"].append({"type": "sns", "config": {"region": "us-east-1", "topic_arn": topic.arn, "template": "{{range .}}{{range .Fields}}{{.Key}}:{{.Count}} {{end}}{{end}}"}})
        rules[key] = rule
        daemon.add_rule(f"{key}.json", rule)
    run_once(daemon, cluster, [cleaned(rule["name"]) for rule in rules.values()])

    recent = [docs[2], docs[1], docs[0]]
    (line,) = read_lines(out / "busy.jsonl")
    assert line["rule_name"] == rules["busy"]["name"]
    same(
        line["results"],
        [
            {"filter": "aggregations.hostname.buckets", "fields": [{"key": "host-a", "doc_count": 2}, {"key": "host-b", "doc_count": 1}]},
            {"filter": "hits.hits._source", "text": body_text(*(syslog_text(doc) for doc in recent))},
        ],
    )
    assert topic.messages(1) == [f"[{rules['busy']['name']}]\nhost-a:2 host-b:1 "]
    # Values outside the query's response (older or other-source entries) cannot satisfy a condition.
    assert not (out / "overloaded.jsonl").exists()
    check_state_doc(cluster.state_docs(cleaned(rules["busy"]["name"]))[0], cleaned(rules["busy"]["name"]), recent)
    check_state_doc(cluster.state_docs(cleaned(rules["overloaded"]["name"]))[0], cleaned(rules["overloaded"]["name"]), [])


def test_rules_directory_selection(cluster, root, logs, daemon):
    out = output_dir(root)
    suffix = tag()
    info = {"query": {"term": {"level": "info"}}, "_source": ["msg"]}
    loaded = [file_rule(f"Loaded {key} {suffix}", logs, info, out, f"{key}.jsonl") for key in ("one", "two")]
    daemon.add_rule("one.json", loaded[0])
    daemon.add_rule("two.json", loaded[1])
    ignored = file_rule(f"Ignored {suffix}", logs, info, out, "ignored.jsonl")
    daemon.add_rule("three.json.disabled", ignored)
    daemon.add_rule("NOTES.txt", "free text, not JSON")
    nested = daemon.rules / "nested"
    nested.mkdir(mode=0o755)
    (nested / "four.json").write_text('{"name": "broken')
    (nested / "four.json").chmod(0o644)
    run_once(daemon, cluster, [cleaned(rule["name"]) for rule in loaded])
    for key in ("one", "two"):
        assert len(read_lines(out / f"{key}.jsonl")) == 1
    assert not (out / "ignored.jsonl").exists()
    assert cluster.state_docs(cleaned(ignored["name"])) == []


def test_renamed_rule_and_state_across_restarts(cluster, root, logs):
    out = output_dir(root)
    suffix = tag()
    info = {"query": {"term": {"level": "info"}}, "_source": ["msg"]}

    def run(name, filename, wait_for_state):
        daemon = Daemon(root, cluster)
        daemon.add_rule("rule.json", file_rule(name, logs, info, out, filename))
        if wait_for_state:
            run_once(daemon, cluster, [cleaned(name)])
        else:
            daemon.start()
            time.sleep(4)
            assert daemon.stop() == 0
        cluster.refresh_state()

    run(f"Ops Alert {suffix}", "first.jsonl", True)
    assert len(read_lines(out / "first.jsonl")) == 1
    # A name that normalizes identically shares the recorded schedule, so it waits.
    run(f"OPS ALERT {suffix}", "second.jsonl", False)
    assert not (out / "second.jsonl").exists()
    assert len(cluster.state_docs(f"ops-alert-{suffix}")) == 1
    # A different name has no state and runs at once.
    run(f"Ops Alert {suffix} v2", "third.jsonl", True)
    (line,) = read_lines(out / "third.jsonl")
    assert line["rule_name"] == f"Ops Alert {suffix} v2"
    assert len(cluster.state_docs(f"ops-alert-{suffix}")) == 1
    assert len(cluster.state_docs(f"ops-alert-{suffix}-v2")) == 1
