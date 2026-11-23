"""Invocation, configuration sources and configuration validation."""

import copy
import time

import pytest

from forwarder import Case, assert_documents, bodies, config, now_ms, rfc3339_seconds, to_yaml
from harness import run_program, workspace

DUMMY = "http://127.0.0.1:9"  # validation never contacts OpenSearch


@pytest.fixture
def offline():
    """A scratch Case without AWS resources, for configuration checks."""
    with workspace() as root:
        yield Case(root, search=None)


def base():
    return config(DUMMY, named={"/fwd/validate/app": {}}, exporter={"dataset": "v", "namespace": "n"})


def edit(document, path, value):
    document = copy.deepcopy(document)
    *parents, last = path.split("::")
    node = document
    for key in parents:
        node = node.setdefault(key, {})
    if value is DELETE:
        del node[last]
    else:
        node[last] = value
    return document


DELETE = object()
LOGS = "receivers::aws_cloudwatch::logs"
EXPORTER = "exporters::opensearch"


def test_validate_accepts_full_configuration(offline):
    storage = offline.storage()
    document = config(
        DUMMY,
        autodiscover={"prefix": "/fwd/", "limit": 5, "streams": {"names": ["a"], "prefixes": ["web-"]}},
        storage=storage,
        logs={"poll_interval": "2s", "initial_lookback": "1h", "max_events_per_request": 50},
        exporter={
            "dataset": "app",
            "namespace": "prod",
            "logs_index": "logs-%{cloudwatch.log.stream}",
            "logs_index_fallback": "other",
            "logs_index_time_format": "yyyy.MM.dd",
            "bulk_action": "index",
            "pipeline": "p1",
            "retry_on_failure": {"enabled": True, "initial_interval": "1s", "max_interval": "5s", "max_elapsed_time": "30s"},
        },
    )
    result = offline.validate(offline.write_config("config.yaml", document))
    assert result.returncode == 0, result.stderr
    named = edit(base(), LOGS + "::groups", {"named": {"/fwd/a": {"names": ["x", "y"]}, "/fwd/b": {"prefixes": ["p"]}}})
    named = edit(named, LOGS + "::start_from", "2024-01-02T15:04:05Z")
    result = offline.validate(offline.write_config("named.yaml", named))
    assert result.returncode == 0, result.stderr


RECEIVER_ERRORS = {
    "poll-interval-below-one-second": (LOGS + "::poll_interval", "500ms"),
    "zero-events-per-request": (LOGS + "::max_events_per_request", 0),
    "named-and-autodiscover": (LOGS + "::groups", {"named": {"/fwd/a": {}}, "autodiscover": {"prefix": "/fwd/"}}),
    "prefix-and-pattern": (LOGS + "::groups", {"autodiscover": {"prefix": "/fwd/", "pattern": "app"}}),
    "zero-autodiscover-limit": (LOGS + "::groups", {"autodiscover": {"prefix": "/fwd/", "limit": 0}}),
    "negative-lookback": (LOGS + "::initial_lookback", "-1h"),
    "malformed-start-from": (LOGS + "::start_from", "2024-01-02 15:04"),
    "unknown-logs-key": (LOGS + "::poll_intervl", "2s"),
    "missing-region": ("receivers::aws_cloudwatch::region", DELETE),
}


@pytest.mark.parametrize("name", sorted(RECEIVER_ERRORS))
def test_validate_rejects_receiver_settings(offline, name):
    path, value = RECEIVER_ERRORS[name]
    result = offline.validate(offline.write_config("config.yaml", edit(base(), path, value)))
    assert result.returncode != 0, f"{name} was accepted"
    assert "aws_cloudwatch" in result.stderr, result.stderr


def test_validate_rejects_both_start_options(offline):
    document = edit(edit(base(), LOGS + "::start_from", "2024-01-02T15:04:05Z"), LOGS + "::initial_lookback", "1h")
    result = offline.validate(offline.write_config("config.yaml", document))
    assert result.returncode != 0
    assert "aws_cloudwatch" in result.stderr, result.stderr


def test_unquoted_start_from_is_not_a_string(offline):
    document = edit(base(), LOGS + "::start_from", "2024-01-02T15:04:05Z")
    quoted = offline.write_config("quoted.yaml", document)
    assert offline.validate(quoted).returncode == 0
    raw = quoted.read_text().replace('"2024-01-02T15:04:05Z"', "2024-01-02T15:04:05Z")
    assert "start_from: 2024-01-02T15:04:05Z" in raw
    result = offline.validate(offline.write_config("plain.yaml", raw.rstrip("\n")))
    assert result.returncode != 0
    assert "aws_cloudwatch" in result.stderr, result.stderr


EXPORTER_ERRORS = {
    "bulk-action-update": (EXPORTER + "::bulk_action", "update"),
    "time-format-slash": (EXPORTER + "::logs_index_time_format", "yyyy/MM"),
    "time-format-uppercase-year": (EXPORTER + "::logs_index_time_format", "YYYY"),
    "empty-dataset": (EXPORTER + "::dataset", ""),
    "empty-namespace": (EXPORTER + "::namespace", ""),
    "unknown-mapping-mode": (EXPORTER + "::mapping", {"mode": "nope"}),
    "missing-endpoint": (EXPORTER + "::http", {"timeout": "5s"}),
    "retry-elapsed-below-interval": (EXPORTER + "::retry_on_failure", {"max_interval": "10s", "max_elapsed_time": "5s"}),
}


@pytest.mark.parametrize("name", sorted(EXPORTER_ERRORS))
def test_validate_rejects_exporter_settings(offline, name):
    path, value = EXPORTER_ERRORS[name]
    result = offline.validate(offline.write_config("config.yaml", edit(base(), path, value)))
    assert result.returncode != 0, f"{name} was accepted"
    assert "opensearch" in result.stderr, result.stderr


def test_storage_directory_must_exist(offline):
    missing = offline.root / "absent"
    document = config(DUMMY, named={"/fwd/a": {}}, storage=missing)
    result = offline.validate(offline.write_config("config.yaml", document))
    assert result.returncode != 0
    assert "file_storage" in result.stderr, result.stderr
    assert not missing.exists()


def test_missing_config_file_fails(offline):
    result = run_program(["validate", "--config", f"file:{offline.root}/absent.yaml"], cwd=offline.root)
    assert result.returncode != 0
    assert result.stderr.strip()


def test_unset_environment_variable_expands_to_empty(offline):
    document = edit(base(), EXPORTER + "::dataset", "${env:FORWARDER_TEST_DATASET}")
    path = offline.write_config("config.yaml", document)
    result = offline.validate(path)
    assert result.returncode != 0
    assert "opensearch" in result.stderr, result.stderr
    assert offline.validate(path, env={"FORWARDER_TEST_DATASET": "fromenv"}).returncode == 0


def test_invalid_configuration_stops_before_polling(case):
    group = case.group("app", "s")
    case.put(group, "s", (now_ms() - 5000, "never forwarded"))
    document = config(case.search.endpoint, named={group: {}}, logs={"poll_interval": "1s", "max_events_per_request": -1},
                      exporter={"dataset": "bad", "namespace": case.namespace})
    with case.collector(case.write_config("config.yaml", document)) as program:
        status = program.process.wait(timeout=15)
        assert status != 0
        assert "aws_cloudwatch" in program.log()
    assert case.search.indices(f"*{case.id}*") == {}


def test_environment_expansion_and_config_merging(case):
    group = case.group("app", "s")
    now = now_ms()
    case.put(group, "s", (now - 60000, "sixty"), (now - 40000, "forty"), (now - 20000, "twenty"))
    document = config(
        case.search.endpoint,
        named={group: {}},
        logs={"start_from": rfc3339_seconds(now - 30000)},
        exporter={"dataset": "${env:FWD_DATASET}", "namespace": case.namespace},
    )
    path = case.write_config("config.yaml", document)
    with case.collector(path, env={"FWD_DATASET": "fromenv"}) as program:
        assert case.search.wait_count(case.index("fromenv"), 1, 15) >= 1
        time.sleep(2.5)
        program.stop()
    assert bodies(case.search.hits(case.index("fromenv"))) == ["twenty"]

    overrides = [
        "--config", "yaml:exporters: {opensearch: {dataset: merged}}",
        "--config", "yaml:receivers: {aws_cloudwatch: {logs: {start_from: '2000-01-01T00:00:00Z'}}}",
    ]
    with case.collector(path, env={"FWD_DATASET": "fromenv"}, extra_args=overrides) as program:
        assert case.search.wait_count(case.index("merged"), 3, 15) >= 3
        time.sleep(2.5)
        program.stop()
    assert bodies(case.search.hits(case.index("merged"))) == ["forty", "sixty", "twenty"]
    assert case.search.indices(f"*{case.id}*") == {case.index("fromenv"): 1, case.index("merged"): 3}


def test_config_from_environment_source(case):
    group = case.group("app", "s")
    case.put(group, "s", (now_ms() - 10000, "via env source"))
    document = config(case.search.endpoint, named={group: {}}, exporter={"dataset": "envsrc", "namespace": case.namespace})
    text = to_yaml(document) + "\n"
    started = now_ms()
    with case.collector(None, env={"FWD_CONFIG": text}, extra_args=["--config", "env:FWD_CONFIG"]) as program:
        assert case.search.wait_count(case.index("envsrc"), 1, 15) >= 1
        time.sleep(2.5)
        assert program.stop() == 0
    event = case.events(group)["via env source"]
    assert_documents(case.search.hits(f"*{case.id}*"), [case.expected(event, "envsrc")], started)
