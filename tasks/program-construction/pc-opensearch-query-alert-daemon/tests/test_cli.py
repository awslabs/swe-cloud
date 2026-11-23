"""Invocation, flags, signals and start-up validation of the configuration and rule files."""

import json
import signal
import time

import pytest

from alertkit import Daemon, cleaned, output_dir, read_lines, tag, wait_lines, wait_state
from harness import run_program

BANNER = "Go Elasticsearch Alerts version 0.2.5, commit e4914b2, built unknown\n"


def test_version_banner():
    result = run_program(["-version"])
    assert result.returncode == 0
    assert result.stdout == BANNER


def test_help_flag_lists_version():
    result = run_program(["-h"])
    assert result.returncode == 0
    assert "-version" in result.stdout + result.stderr


def test_unknown_flag_is_a_usage_error():
    result = run_program(["-no-such-flag"])
    assert result.returncode == 2
    assert result.stdout == ""


def test_default_configuration_path_is_absent():
    # Without the environment variables the daemon reads /etc/go-elasticsearch-alerts/config.json,
    # which does not exist in the venue.
    result = run_program([], timeout=15)
    assert result.returncode == 1
    assert result.stdout == ""


def valid_rule(out):
    return {
        "name": "Validation Rule",
        "index": "missing-" + tag(),
        "schedule": "@every 1h",
        "body": {"query": {"match_all": {}}},
        "outputs": [{"type": "file", "config": {"file": str(out / "alerts.jsonl")}}],
    }


def without(key):
    return lambda rule: {k: v for k, v in rule.items() if k != key}


def setting(key, value):
    return lambda rule: {**rule, key: value}


def output(config_or_output):
    return lambda rule: {**rule, "outputs": [config_or_output]}


def sns(**config):
    return output({"type": "sns", "config": config})


SNS_OK = {"region": "us-east-1", "topic_arn": "arn:aws:sns:us-east-1:000000000000:validation", "template": "x"}


def condition(**fields):
    return lambda rule: {**rule, "conditions": [{"field": "hits.total.value", "gt": 0}, fields]}


RULE_ERRORS = {
    "missing-name": without("name"),
    "empty-name": setting("name", ""),
    "missing-index": without("index"),
    "missing-schedule": without("schedule"),
    "missing-body": without("body"),
    "body-number": setting("body", 5),
    "body-array": setting("body", [{"query": {"match_all": {}}}]),
    "body-string-not-json": setting("body", "{query"),
    "body-empty-object": setting("body", {}),
    "body-string-empty-object": setting("body", "{}"),
    "missing-outputs": without("outputs"),
    "empty-outputs": setting("outputs", []),
    "output-without-type": output({"config": {"file": "/tmp/x"}}),
    "output-without-config": output({"type": "file"}),
    "output-empty-config": output({"type": "file", "config": {}}),
    "output-unknown-type": output({"type": "pager", "config": {"to": "x"}}),
    "file-without-path": output({"type": "file", "config": {"path": "/tmp/x"}}),
    "sns-without-region": sns(**{k: v for k, v in SNS_OK.items() if k != "region"}),
    "sns-without-topic": sns(**{k: v for k, v in SNS_OK.items() if k != "topic_arn"}),
    "sns-without-template": sns(**{k: v for k, v in SNS_OK.items() if k != "template"}),
    "sns-template-syntax": sns(**{**SNS_OK, "template": "{{range .}}{{.Filter}}"}),
    "schedule-words": setting("schedule", "every five minutes"),
    "schedule-four-fields": setting("schedule", "* * * *"),
    "schedule-seven-fields": setting("schedule", "0 0 */3 * * * *"),
    "schedule-bad-duration": setting("schedule", "@every soon"),
    "condition-without-field": condition(gt=1),
    "condition-empty-field": condition(field="", gt=1),
    "condition-unknown-quantifier": condition(field="hits.total.value", quantifier="most", gt=1),
    "condition-quantifier-not-string": condition(field="hits.total.value", quantifier=1, gt=1),
    "condition-gt-string": condition(field="hits.total.value", gt="1"),
    "condition-le-boolean": condition(field="hits.total.value", le=True),
    "condition-eq-boolean": condition(field="hits.total.value", eq=True),
    "condition-ne-object": condition(field="hits.total.value", ne={"a": 1}),
    "condition-eq-empty-string": condition(field="hits.total.value", eq=""),
}


@pytest.mark.parametrize("case", sorted(RULE_ERRORS))
def test_invalid_rule_is_rejected_at_start(case, cluster, root):
    out = output_dir(root)
    daemon = Daemon(root, cluster)
    daemon.add_rule("rule.json", RULE_ERRORS[case](valid_rule(out)))
    assert daemon.run_to_exit(timeout=15) == 1
    assert not (out / "alerts.jsonl").exists()


def test_malformed_rule_json_is_rejected(cluster, root):
    daemon = Daemon(root, cluster)
    daemon.add_rule("rule.json", '{"name": "Broken", "index": "x",')
    assert daemon.run_to_exit() == 1


@pytest.mark.parametrize(
    "case",
    ["missing-file", "not-json", "no-elasticsearch", "no-server", "no-url", "distributed-without-consul", "tls-without-certificates"],
)
def test_invalid_main_configuration_is_rejected(case, cluster, root):
    out = output_dir(root)
    daemon = Daemon(root, cluster)
    daemon.add_rule("rule.json", valid_rule(out))
    url = cluster.url
    configs = {
        "not-json": '{"elasticsearch": {"server": ',
        "no-elasticsearch": {"distributed": False},
        "no-server": {"elasticsearch": {"client": {"tls_enabled": False}}},
        "no-url": {"elasticsearch": {"server": {}}},
        "distributed-without-consul": {"elasticsearch": {"server": {"url": url}}, "distributed": True},
        "tls-without-certificates": {"elasticsearch": {"server": {"url": url}, "client": {"tls_enabled": True}}},
    }
    if case == "missing-file":
        daemon.config.unlink()
    else:
        daemon.write_config(configs[case])
    assert daemon.run_to_exit() == 1
    assert not (out / "alerts.jsonl").exists()


@pytest.mark.parametrize("variable", ["GO_ELASTICSEARCH_ALERTS_ES_USERNAME", "GO_ELASTICSEARCH_ALERTS_ES_PASSWORD"])
def test_half_configured_basic_auth_is_rejected(variable, cluster, root):
    out = output_dir(root)
    daemon = Daemon(root, cluster)
    daemon.add_rule("rule.json", valid_rule(out))
    assert daemon.run_to_exit(env={variable: "alerts"}) == 1


@pytest.mark.parametrize("layout", ["empty", "missing", "only-other-files"])
def test_rules_directory_without_rules_is_rejected(layout, cluster, root):
    daemon = Daemon(root, cluster)
    if layout == "missing":
        daemon.rules.rmdir()
    elif layout == "only-other-files":
        daemon.add_rule("rule.json.disabled", valid_rule(output_dir(root)))
        daemon.add_rule("README.txt", "not a rule")
    assert daemon.run_to_exit() == 1


@pytest.fixture
def events(cluster, indices):
    name = "cli-events-" + tag()
    indices.append(name)
    cluster.create_index(name, {"level": {"type": "keyword"}}, [{"level": "error"}, {"level": "info"}])
    return name


def test_one_invalid_rule_prevents_every_rule(cluster, root, events):
    out = output_dir(root)
    good = valid_rule(out)
    good["name"] = "Valid Neighbour " + tag()
    good["index"] = events  # on its own this rule fires at once
    daemon = Daemon(root, cluster)
    daemon.add_rule("a-good.json", good)
    bad = valid_rule(out)
    bad["schedule"] = "@every never"
    daemon.add_rule("b-bad.json", bad)
    assert daemon.run_to_exit() == 1
    assert read_lines(out / "alerts.jsonl") == []
    time.sleep(1)
    assert read_lines(out / "alerts.jsonl") == []
    assert cluster.state_docs(cleaned(good["name"])) == []


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_stop_signals_exit_cleanly(sig, cluster, root):
    out = output_dir(root)
    rule = valid_rule(out)
    rule["name"] = "Signal Rule " + tag()
    daemon = Daemon(root, cluster)
    daemon.add_rule("rule.json", rule)
    daemon.start()
    assert wait_state(cluster, cleaned(rule["name"]))
    assert daemon.running()
    assert daemon.stop(sig) == 0


def test_unreachable_cluster_keeps_running(root):
    out = output_dir(root)
    daemon = Daemon(root, None, config={"elasticsearch": {"server": {"url": "http://127.0.0.1:9/"}}})
    daemon.add_rule("rule.json", {**valid_rule(out), "schedule": "@every 1s"})
    daemon.start()
    try:
        time.sleep(3)
        assert daemon.running()
    finally:
        assert daemon.stop() == 0
    assert not (out / "alerts.jsonl").exists()


def test_configuration_extras_and_trailing_slash(cluster, root, events):
    out = output_dir(root)
    rule = valid_rule(out)
    rule["name"] = "Extras Rule " + tag()
    rule["index"] = events
    rule["body"] = {"query": {"term": {"level": "error"}}, "_source": ["level"]}
    daemon = Daemon(root, cluster, config=json.dumps({"elasticsearch": {"server": {"url": cluster.url + "/"}, "client": {"tls_enabled": False}}, "distributed": False, "comment": "x"}))
    daemon.add_rule("rule.json", rule)
    daemon.start()
    docs = wait_state(cluster, cleaned(rule["name"]))
    assert [doc["hits"] for doc in docs] == [[{"level": "error"}]]
    assert len(wait_lines(out / "alerts.jsonl", 1)) == 1
    assert daemon.stop() == 0


def test_home_relative_paths_are_expanded(cluster, root, events):
    out = output_dir(root)
    rule = valid_rule(out)
    rule["name"] = "Home Paths " + tag()
    rule["index"] = events
    daemon = Daemon(root, cluster)
    daemon.add_rule("rule.json", rule)
    daemon.start(env={"HOME": str(root), "GO_ELASTICSEARCH_ALERTS_CONFIG_FILE": "~/config.json", "GO_ELASTICSEARCH_ALERTS_RULES_DIR": "~/rules"})
    assert wait_state(cluster, cleaned(rule["name"]))
    assert len(wait_lines(out / "alerts.jsonl", 1)) == 1
    assert daemon.stop() == 0
