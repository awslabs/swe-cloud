"""Invocation: `validate` exit statuses, configuration formats, run-time startup and signals."""

import json
import signal
import time

import pytest
from harness import give_to_candidate, run_program, workspace
from pipeline import parse_instant

EX_CONFIG = 78
QUEUE = "http://engines:4566/000000000000/not-a-real-queue"


def toml_config(data_dir, endpoint="http://127.0.0.1:9", extra_source="", extra_sink="", head=""):
    return f"""{head}data_dir = "{data_dir}"

[sources.bucket]
type = "aws_s3"
region = "us-east-1"
endpoint = "http://engines:4566"
{extra_source}

[sources.bucket.sqs]
queue_url = "{QUEUE}"

[sinks.search]
type = "elasticsearch"
inputs = ["bucket"]
endpoints = ["{endpoint}"]
api_version = "v7"
{extra_sink}
"""


def yaml_config(data_dir, endpoint="http://127.0.0.1:9"):
    return f"""data_dir: "{data_dir}"
sources:
  bucket:
    type: aws_s3
    region: us-east-1
    endpoint: "http://engines:4566"
    sqs:
      queue_url: "{QUEUE}"
sinks:
  search:
    type: elasticsearch
    inputs: [bucket]
    endpoints: ["{endpoint}"]
    api_version: v7
"""


def json_config(data_dir, endpoint="http://127.0.0.1:9"):
    return json.dumps(
        {
            "data_dir": str(data_dir),
            "sources": {"bucket": {"type": "aws_s3", "region": "us-east-1", "endpoint": "http://engines:4566", "sqs": {"queue_url": QUEUE}}},
            "sinks": {"search": {"type": "elasticsearch", "inputs": ["bucket"], "endpoints": [endpoint], "api_version": "v7"}},
        }
    )


def write(root, name, text):
    path = root / name
    path.write_text(text)
    path.chmod(0o644)
    return path


def state_dir(root):
    path = root / "state"
    path.mkdir(exist_ok=True)
    give_to_candidate(path)
    return path


def validate(root, *args):
    return run_program(["validate", *map(str, args)], cwd=root)


@pytest.mark.parametrize(
    ("name", "render"),
    [("pipeline.toml", toml_config), ("pipeline.yaml", yaml_config), ("pipeline.yml", yaml_config), ("pipeline.json", json_config)],
)
def test_validate_accepts_each_configuration_format(name, render):
    with workspace() as root:
        path = write(root, name, render(state_dir(root)))
        assert validate(root, "--no-environment", path).returncode == 0


def test_validate_chooses_parser_from_extension():
    with workspace() as root:
        data = state_dir(root)
        yaml_as_toml = write(root, "yaml-content.toml", yaml_config(data))
        json_as_yaml = write(root, "json-content.yaml", json_config(data))
        toml_as_json = write(root, "toml-content.json", toml_config(data))
        assert validate(root, "--no-environment", yaml_as_toml).returncode == EX_CONFIG
        # JSON is a subset of YAML, so a JSON document in a .yaml file is valid.
        assert validate(root, "--no-environment", json_as_yaml).returncode == 0
        assert validate(root, "--no-environment", toml_as_json).returncode == EX_CONFIG


def test_validate_accepts_admitted_options():
    source = """compression = "zstd"
force_path_style = true
decoding.codec = "json"
framing.method = "character_delimited"
framing.character_delimited.delimiter = ";"
multiline.start_pattern = "^[^\\\\s]"
multiline.condition_pattern = "^[\\\\s]+"
multiline.mode = "continue_through"
multiline.timeout_ms = 1000
"""
    sink = """mode = "bulk"
bulk.index = "app-{{ level }}-%Y.%m.%d"
bulk.action = "create"
bulk.template_fallback_index = "app-unrouted"
id_key = "meta.id"
request_retry_partial = true
batch.max_events = 50
batch.timeout_secs = 1
request.retry_attempts = 3
request.retry_initial_backoff_secs = 1
request.retry_max_duration_secs = 5
request.timeout_secs = 20
encoding.except_fields = ["region"]
acknowledgements.enabled = true
healthcheck.enabled = false
"""
    with workspace() as root:
        text = toml_config(state_dir(root), extra_source=source, extra_sink=sink)
        text = text.replace(f'queue_url = "{QUEUE}"', f'queue_url = "{QUEUE}"\npoll_secs = 2\nvisibility_timeout_secs = 45\ndelete_message = true\ndelete_failed_message = false\nmax_number_of_messages = 5\nclient_concurrency = 2')
        assert validate(root, "--no-environment", write(root, "full.toml", text)).returncode == 0
        stream = toml_config(state_dir(root), extra_sink='mode = "data_stream"\ndata_stream.type = "logs"\ndata_stream.dataset = "app"\ndata_stream.namespace = "prod"\ndata_stream.auto_routing = false\ndata_stream.sync_fields = false\n')
        assert validate(root, "--no-environment", write(root, "stream.toml", stream)).returncode == 0


BROKEN = {
    "unknown-api-version": lambda d: toml_config(d).replace('api_version = "v7"', 'api_version = "v9"'),
    "unknown-compression": lambda d: toml_config(d, extra_source='compression = "brotli"'),
    "unknown-codec": lambda d: toml_config(d, extra_source='decoding.codec = "xml"'),
    "missing-queue-url": lambda d: toml_config(d).replace(f'queue_url = "{QUEUE}"', ""),
    "missing-endpoints": lambda d: toml_config(d).replace('endpoints = ["http://127.0.0.1:9"]', ""),
    "dangling-input": lambda d: toml_config(d).replace('inputs = ["bucket"]', 'inputs = ["elsewhere"]'),
    "unknown-top-level-option": lambda d: toml_config(d, head="unrelated_option = 1\n"),
    "unknown-source-option": lambda d: toml_config(d, extra_source="bucket_name = 1"),
    "toml-syntax": lambda d: toml_config(d) + "\n[sinks.search\n",
}


@pytest.mark.parametrize("case", sorted(BROKEN))
def test_validate_rejects_invalid_configuration(case):
    with workspace() as root:
        path = write(root, f"{case}.toml", BROKEN[case](state_dir(root)))
        assert validate(root, "--no-environment", path).returncode == EX_CONFIG


def test_validate_missing_file_is_a_configuration_error():
    with workspace() as root:
        assert validate(root, "--no-environment", root / "absent.toml").returncode == EX_CONFIG


def test_validate_runs_health_checks_unless_disabled(opensearch):
    with workspace() as root:
        data = state_dir(root)
        reachable = write(root, "reachable.toml", toml_config(data, endpoint=opensearch))
        unreachable = write(root, "unreachable.toml", toml_config(data, endpoint="http://127.0.0.1:9"))
        unchecked = write(root, "unchecked.toml", toml_config(data, endpoint="http://127.0.0.1:9", extra_sink="healthcheck.enabled = false"))
        assert validate(root, reachable).returncode == 0
        assert validate(root, unreachable).returncode == EX_CONFIG
        assert validate(root, "--no-environment", unreachable).returncode == 0
        assert validate(root, unchecked).returncode == 0
        # A disabled health check is reported as a warning.
        assert validate(root, "--deny-warnings", unchecked).returncode == EX_CONFIG
        assert validate(root, "--deny-warnings", reachable).returncode == 0


def test_run_with_invalid_configuration_exits_with_config_error():
    with workspace() as root:
        broken = write(root, "broken.toml", BROKEN["unknown-api-version"](state_dir(root)))
        started = time.time()
        assert run_program(["--config", broken], cwd=root, timeout=20).returncode == EX_CONFIG
        assert run_program(["--config", root / "absent.toml"], cwd=root, timeout=20).returncode == EX_CONFIG
        assert time.time() - started < 20


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_running_pipeline_stops_cleanly_on_signal(pipe, sig):
    pipe.start(pipe.config())
    time.sleep(3)
    assert pipe.alive()
    assert pipe.stop(sig) == 0


def test_yaml_configuration_with_short_flag_runs_pipeline(pipe):
    head = pipe.put("short/flag.log", b"from yaml\n")
    pipe.notify("short/flag.log")
    data = pipe.root / "yaml-state"
    data.mkdir()
    give_to_candidate(data)
    text = f"""data_dir: "{data}"
sources:
  in:
    type: aws_s3
    region: us-east-1
    endpoint: "http://engines:4566"
    sqs:
      queue_url: "{pipe.queue}"
      poll_secs: 1
sinks:
  out:
    type: elasticsearch
    inputs: [in]
    endpoints: ["{pipe.os}"]
    api_version: v7
    bulk:
      index: "{pipe.prefix}-yaml"
    batch:
      timeout_secs: 1
    acknowledgements:
      enabled: true
"""
    path = pipe.config(text=text, suffix=".yaml")
    pipe.start(path, flag="-c")
    (hit,) = pipe.wait_settled(1)
    assert hit["_index"] == f"{pipe.prefix}-yaml"
    assert hit["_source"]["message"] == "from yaml"
    assert hit["_source"]["object"] == "short/flag.log"
    assert parse_instant(hit["_source"]["timestamp"]) == head["LastModified"]
    assert pipe.stop() == 0
