"""`validate` and `config validate`: offline config checks, their report and exit status.

Adapted from upstream internal/cli/{validate,config,exit}_test.go and
internal/validate/*_test.go (template variables, issue-120 did-you-mean,
postgres sink key rules, window shape rules).
"""

import json
import re

import pytest

from harness import run_program, write_fixture

BASE = """\
pipeline:
  name: demo
  batch_size: 2
  source:
    type: kafka
    kafka:
      brokers: [{{ SQLFLOW_KAFKA_BROKERS }}]
      group_id: {{ SQLFLOW_GROUP_ID|default('g') }}
      auto_offset_reset: earliest
      topics: [{{ SQLFLOW_INPUT_TOPIC }}]
  handler:
    type: handlers.InferredMemBatch
    sql: |
      SELECT k, v FROM batch
  sink:
    type: kafka
    kafka:
      brokers: [{{ SQLFLOW_KAFKA_BROKERS }}]
      topic: {{ SQLFLOW_OUT }}
"""
ENV = {"SQLFLOW_KAFKA_BROKERS": "b:9092", "SQLFLOW_INPUT_TOPIC": "i", "SQLFLOW_OUT": "o"}


def validate(root, name, text, *args, env=None, command=("validate",)):
    write_fixture(root / name, text)
    return run_program([*command, name, *args], cwd=root, env=env if env is not None else ENV)


DIAGNOSTIC = re.compile(r"^(\S+:\d+:\d+: )?(error|warning): \[")


def graded(stdout):
    """The graded lines of validate's report: diagnostics, did-you-mean, unused variables and the valid line."""
    lines = []
    for line in stdout.splitlines():
        if line.strip().startswith("did you mean:"):
            lines.append(line.strip())
        elif DIAGNOSTIC.match(line) or line.startswith("supplied but never read:") or line.endswith(": valid"):
            lines.append(line)
    return lines


def assert_report(stdout, expected):
    """A diagnostic must start with its expected prefix and leading message; other graded lines match exactly."""
    lines = graded(stdout)
    assert len(lines) == len(expected), f"graded lines {lines} differ from {expected}"
    for line, want in zip(lines, expected):
        if DIAGNOSTIC.match(want):
            assert line.startswith(want), f"{line!r} does not start with {want!r}"
        else:
            assert line == want, f"{line!r} != {want!r}"


def assert_config_invalid(result, name, messages):
    """config validate: an Error line naming the config, then one indented line per error message."""
    assert result.returncode == 10, result.stderr
    assert not result.stdout.strip().endswith(": valid")
    lines = [line for line in result.stderr.splitlines() if line.strip()]
    assert lines and lines[0].startswith("Error: [user.config.invalid] ") and name in lines[0], result.stderr
    rest = [line.strip() for line in lines[1:]]
    assert len(rest) == len(messages), result.stderr
    for line, want in zip(rest, messages):
        assert line.startswith(want), f"{line!r} does not start with {want!r}"


def assert_config_valid(result, name):
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [f"{name}: valid"]


# ------------------------------------------------------------------ valid configs
def test_config_validate_prints_valid(scratch):
    assert_config_valid(validate(scratch, "base.yml", BASE, command=("config", "validate")), "base.yml")


def test_validate_valid_config_report(scratch):
    result = validate(scratch, "base.yml", BASE)
    assert result.returncode == 0, result.stdout + result.stderr
    assert_report(result.stdout, ["base.yml: valid"])


def test_validate_lists_supplied_variables_never_read(scratch):
    env = {**ENV, "SQLFLOW_EXTRA_ONE": "1", "SQLFLOW_ANOTHER": "x", "SQLFLOW_GROUP_ID": "grp", "OTHER_VAR": "1"}
    result = validate(scratch, "base.yml", BASE, env=env)
    assert result.returncode == 0, result.stdout
    assert_report(result.stdout, ["supplied but never read: [SQLFLOW_ANOTHER SQLFLOW_EXTRA_ONE]", "base.yml: valid"])


def test_validate_json_report_for_a_valid_config(scratch):
    result = validate(scratch, "base.yml", BASE, "--json", env={**ENV, "SQLFLOW_EXTRA_ONE": "1"})
    assert result.returncode == 0, result.stdout
    report = json.loads(result.stdout)
    assert report["config"] == "base.yml"
    assert report["ok"] is True
    assert report["diagnostics"] == []
    variables = report["variables"]
    assert sorted(variables["referenced"]) == ["SQLFLOW_GROUP_ID", "SQLFLOW_INPUT_TOPIC", "SQLFLOW_KAFKA_BROKERS", "SQLFLOW_OUT"]
    assert variables["missing"] == []
    assert variables["unused"] == ["SQLFLOW_EXTRA_ONE"]
    assert {"SQLFLOW_EXTRA_ONE", "SQLFLOW_INPUT_TOPIC", "SQLFLOW_KAFKA_BROKERS", "SQLFLOW_OUT"} <= set(variables["provided"])


def test_batch_size_is_optional_to_validate(scratch):
    text = BASE.replace("  batch_size: 2\n", "")
    result = validate(scratch, "nobatch.yml", text)
    assert result.returncode == 0, result.stdout
    assert_report(result.stdout, ["nobatch.yml: valid"])


# ------------------------------------------------------------------ schema faults
UNKNOWN_PIPELINE_KEY = BASE.replace("  batch_size: 2\n", "  batch_size: 2\n  bogus_key: 1\n")


def test_config_validate_unknown_key(scratch):
    result = validate(scratch, "unk.yml", UNKNOWN_PIPELINE_KEY, command=("config", "validate"))
    assert_config_invalid(result, "unk.yml", ["at '/pipeline': additional properties 'bogus_key' not allowed"])


def test_validate_unknown_key_diagnostic(scratch):
    result = validate(scratch, "unk.yml", UNKNOWN_PIPELINE_KEY)
    assert result.returncode == 10
    assert_report(result.stdout, ["unk.yml:1:1: error: [user.config.invalid] at '/pipeline': additional properties 'bogus_key' not allowed"])


def test_validate_unknown_nested_and_top_level_keys(scratch):
    nested = BASE.replace("      topic: {{ SQLFLOW_OUT }}\n", "      topic: {{ SQLFLOW_OUT }}\n      partition: 3\n")
    result = validate(scratch, "nested.yml", nested)
    assert result.returncode == 10
    assert_report(result.stdout, [
        "nested.yml:17:5: error: [user.config.invalid] at '/pipeline/sink/kafka': additional properties 'partition' not allowed"])

    top = validate(scratch, "top.yml", "extra_top: 1\n" + BASE, command=("config", "validate"))
    assert_config_invalid(top, "top.yml", ["at '': additional properties 'extra_top' not allowed"])


def test_validate_wrong_type_and_missing_block(scratch):
    result = validate(scratch, "type.yml", BASE.replace("batch_size: 2", "batch_size: abc"))
    assert result.returncode == 10
    assert_report(result.stdout, ["type.yml:3:3: error: [user.config.invalid] at '/pipeline/batch_size': got string, want integer"])

    text = BASE.replace("  handler:\n    type: handlers.InferredMemBatch\n    sql: |\n      SELECT k, v FROM batch\n", "")
    missing = validate(scratch, "nohandler.yml", text)
    assert missing.returncode == 10
    assert_report(missing.stdout, ["nohandler.yml:1:1: error: [user.config.invalid] at '/pipeline': missing property 'handler'"])


def test_validate_reports_every_fault_in_one_pass(scratch):
    text = BASE.replace("type: kafka\n", "type: kafak\n")
    result = validate(scratch, "types.yml", text)
    assert result.returncode == 10
    assert_report(result.stdout, ["types.yml:5:5: error: [user.config.invalid] at '/pipeline/source/type': ",
                                  "types.yml:16:5: error: [user.config.invalid] at '/pipeline/sink/type': "])


@pytest.mark.parametrize(("name", "edit", "prefix", "fragment"), [
    ("policy.yml", ("  batch_size: 2\n", "  batch_size: 2\n  on_error:\n    policy: FOO\n"),
     "policy.yml:5:5: error: [user.config.invalid] ", "at '/pipeline/on_error/policy': "),
    ("reset.yml", ("auto_offset_reset: earliest", "auto_offset_reset: middle"),
     "reset.yml:9:7: error: [user.config.invalid] ", "at '/pipeline/source/kafka/auto_offset_reset': "),
])
def test_validate_enum_values(scratch, name, edit, prefix, fragment):
    result = validate(scratch, name, BASE.replace(*edit))
    assert result.returncode == 10
    assert_report(result.stdout, [prefix + fragment])


def test_invalid_yaml_and_template_are_parse_failures(scratch):
    yaml_result = validate(scratch, "bad.yaml", "pipeline:\n  batch_size: [1\n", env={})
    assert yaml_result.returncode == 10
    assert_report(yaml_result.stdout, ["error: [user.config.parse_failed] the config is not valid YAML"])

    tpl = validate(scratch, "badtpl.yml", "pipeline:\n  batch_size: {{ SQLFLOW_X \n", env={})
    assert tpl.returncode == 10
    assert_report(tpl.stdout, ["error: [user.config.parse_failed] the config is not a valid template"])

    plain = validate(scratch, "bad.yaml", "pipeline:\n  batch_size: [1\n", env={}, command=("config", "validate"))
    assert_config_invalid(plain, "bad.yaml", ["the config is not valid YAML"])


@pytest.mark.parametrize("command", [("validate",), ("config", "validate"), ("run",)])
def test_missing_config_file(scratch, command):
    result = run_program([*command, "missing.yml"], cwd=scratch, env={})
    assert result.returncode == 10
    assert not result.stdout.strip().endswith(": valid")
    first = next(line for line in result.stderr.splitlines() if line.startswith("Error: "))
    assert first.startswith("Error: [user.config.not_found] ") and "missing.yml" in first, result.stderr


def test_usage_errors_exit_one(scratch):
    write_fixture(scratch / "a.yml", BASE)
    assert run_program(["validate"], cwd=scratch, env={}).returncode == 1
    assert run_program(["config", "validate", "a.yml", "a.yml"], cwd=scratch, env={}).returncode == 1
    assert run_program(["validate", "a.yml", "--bogus"], cwd=scratch, env={}).returncode == 1


# ------------------------------------------------------------- template variables
def test_unset_variable_is_a_warning_that_demotes_its_line(scratch):
    env = {"SQLFLOW_KAFKA_BROKERS": "b:9092", "SQLFLOW_INPUT_TOPIC": "i"}
    result = validate(scratch, "base.yml", BASE, env=env)
    assert result.returncode == 0, result.stdout
    # The empty topic is a schema fault on the same line as the unset variable, so it is only a warning too.
    assert_report(result.stdout, [
        "base.yml:19:7: warning: [user.config.invalid] at '/pipeline/sink/kafka/topic': got null, want string",
        "base.yml:19:17: warning: [user.config.template_undefined] template variable SQLFLOW_OUT is not set and renders as an empty string",
        "base.yml: valid",
    ])

    assert_config_valid(validate(scratch, "base.yml", BASE, env=env, command=("config", "validate")), "base.yml")

    report = json.loads(validate(scratch, "base.yml", BASE, "--json", env=env).stdout)
    assert report["ok"] is True
    assert [(d["severity"], d["code"], d["position"]["line"], d["position"]["column"]) for d in report["diagnostics"]] == [
        ("warning", "user.config.invalid", 19, 7), ("warning", "user.config.template_undefined", 19, 17)]
    assert report["variables"]["missing"] == ["SQLFLOW_OUT"]


def test_misspelled_variable_is_an_error_with_suggestion(scratch):
    env = {"SQLFLOW_KAFKA_BROKERS": "b:9092", "SQLFLOW_OUT": "o", "SQLFLOW_INPUT_TOPC": "i"}
    result = validate(scratch, "base.yml", BASE, env=env)
    assert result.returncode == 10
    message = ("template variable SQLFLOW_INPUT_TOPIC is not defined and renders as an empty string, "
               "but a similar name is supplied and never read")
    assert_report(result.stdout, [f"base.yml:10:19: error: [user.config.template_undefined] {message}",
                                  "did you mean: [SQLFLOW_INPUT_TOPC]", "supplied but never read: [SQLFLOW_INPUT_TOPC]"])

    report = json.loads(validate(scratch, "base.yml", BASE, "--json", env=env).stdout)
    assert report["ok"] is False
    [diagnostic] = report["diagnostics"]
    assert diagnostic["code"] == "user.config.template_undefined"
    assert diagnostic["class"] == "user"
    assert diagnostic["severity"] == "error"
    assert (diagnostic["position"]["line"], diagnostic["position"]["column"]) == (10, 19)
    assert diagnostic["did_you_mean"] == ["SQLFLOW_INPUT_TOPC"]
    assert report["variables"]["missing"] == ["SQLFLOW_INPUT_TOPIC"]
    assert report["variables"]["unused"] == ["SQLFLOW_INPUT_TOPC"]

    plain = validate(scratch, "base.yml", BASE, env=env, command=("config", "validate"))
    assert_config_invalid(plain, "base.yml", [message])


DEFAULTS = """\
pipeline:
  batch_size: {{ SQLFLOW_BATCH|default(3) }}
  source:
    type: kafka
    kafka:
      brokers: [{{ SQLFLOW_KAFKA_BROKERS|default('localhost:9092') }}]
      group_id: g
      auto_offset_reset: {{ SQLFLOW_RESET|default('latest') }}
      topics: [t]
  handler:
    type: handlers.InferredMemBatch
    sql: SELECT 1
  sink:
    type: noop
"""


def test_defaults_render_and_supplied_values_are_checked(scratch):
    result = validate(scratch, "tpl.yml", DEFAULTS, env={})
    assert result.returncode == 0
    assert_report(result.stdout, ["tpl.yml: valid"])

    batch = validate(scratch, "tpl.yml", DEFAULTS, env={"SQLFLOW_BATCH": "x"})
    assert batch.returncode == 10
    assert_report(batch.stdout, ["tpl.yml:2:3: error: [user.config.invalid] at '/pipeline/batch_size': got string, want integer"])

    reset = validate(scratch, "tpl.yml", DEFAULTS, env={"SQLFLOW_RESET": "middle"}, command=("config", "validate"))
    assert_config_invalid(reset, "tpl.yml", ["at '/pipeline/source/kafka/auto_offset_reset': "])


# ------------------------------------------------------------- postgres sink rules
def postgres_config(mode, key=None):
    text = BASE.split("  sink:\n")[0] + ("  sink:\n    type: postgres\n    postgres:\n"
                                         '      dsn: "postgresql://u:p@h:5432/d"\n      table: t\n'
                                         f"      mode: {mode}\n")
    if key:
        text += f"      key: [{key}]\n"
    return text


@pytest.mark.parametrize(("mode", "key", "message"), [
    ("upsert", None, "pipeline.sink: postgres mode upsert needs key, the columns a row is identified by"),
    ("append", "k", "pipeline.sink: postgres mode append takes no key; every row is inserted as it is"),
])
def test_postgres_sink_key_rules(scratch, mode, key, message):
    env = {k: v for k, v in ENV.items() if k != "SQLFLOW_OUT"}
    result = validate(scratch, "pg.yml", postgres_config(mode, key), env=env)
    assert result.returncode == 10
    assert_report(result.stdout, [f"pg.yml:18:7: error: [user.config.invalid] {message}"])

    plain = validate(scratch, "pg.yml", postgres_config(mode, key), env=env, command=("config", "validate"))
    assert_config_invalid(plain, "pg.yml", [message])


def test_postgres_sink_valid_and_incomplete(scratch):
    env = {k: v for k, v in ENV.items() if k != "SQLFLOW_OUT"}
    result = validate(scratch, "pg.yml", postgres_config("upsert", "k"), env=env)
    assert result.returncode == 0
    assert_report(result.stdout, ["pg.yml: valid"])

    bad_mode = validate(scratch, "mode.yml", postgres_config("replace", "k"), env=env)
    assert bad_mode.returncode == 10
    assert_report(bad_mode.stdout, ["mode.yml:20:7: error: [user.config.invalid] at '/pipeline/sink/postgres/mode': "])

    block = BASE.split("  sink:\n")[0] + "  sink:\n    type: postgres\n"
    missing = validate(scratch, "block.yml", block, env=env)
    assert missing.returncode == 10
    assert_report(missing.stdout, [
        "block.yml:16:5: error: [user.config.invalid] pipeline.sink: type postgres needs a postgres block with dsn, table and mode"])


# -------------------------------------------------------------------- window rules
def window_config(coltype="TIMESTAMPTZ", interval="1 hour", emit="SELECT * FROM closed", lateness=0, idle=60, flush=30,
                  event_time=True):
    text = f"""\
tables:
  sql:
    - name: agg
      sql: |
        CREATE TABLE agg (bucket {coltype}, city VARCHAR, count BIGINT);
      window:
        time_column: bucket
        size_seconds: 3600
        grace_seconds: 0
        idle_close_seconds: {idle}
        allowed_lateness_seconds: {lateness}
        emit_sql: {emit}
        sink:
          type: kafka
          kafka:
            brokers: [b:9092]
            topic: out
pipeline:
  batch_size: 2
  flush_interval_seconds: {flush}
  source:
    type: kafka
    kafka:
      brokers: [b:9092]
      group_id: g
      auto_offset_reset: earliest
      topics: [in]
"""
    if event_time:
        text += "      event_time: {path: timestamp, format: rfc3339}\n"
    return text + f"""\
  handler:
    type: handlers.InferredMemBatch
    sql: |
      INSERT INTO agg BY NAME
      SELECT time_bucket(INTERVAL '{interval}', event_time) AS bucket, properties.city AS city, count(*) AS count
      FROM batch GROUP BY bucket, city
  sink:
    type: noop
"""


def test_window_config_valid(scratch):
    for name, text in [("w.yml", window_config()), ("secs.yml", window_config(interval="3600 seconds"))]:
        result = validate(scratch, name, text, env={})
        assert result.returncode == 0, result.stdout
        assert_report(result.stdout, [f"{name}: valid"])


@pytest.mark.parametrize(("options", "prefix", "fragments"), [
    ({"coltype": "TIMESTAMP"}, "w.yml:7:9: error: [user.config.invalid] ",
     ['tables.sql[0] window: time_column "bucket" is not declared as TIMESTAMPTZ']),
    ({"interval": "30 minutes"}, "w.yml:7:9: error: [user.config.invalid] ",
     ["tables.sql[0] window: time_column \"bucket\" must be computed as time_bucket(INTERVAL '3600 seconds', event_time) "
      "in the handler's SQL"]),
    ({"emit": "SELECT 1"}, "w.yml:12:9: error: [user.config.invalid] ", ["tables.sql[0] window: emit_sql does not read closed"]),
    ({"lateness": 60}, "w.yml:11:9: error: [user.config.invalid] ",
     ["tables.sql[0] window: allowed_lateness_seconds is 60 and the kafka sink appends"]),
])
def test_window_shape_errors(scratch, options, prefix, fragments):
    result = validate(scratch, "w.yml", window_config(**options), env={})
    assert result.returncode == 10
    assert_report(result.stdout, [prefix + fragment for fragment in fragments])


@pytest.mark.parametrize(("options", "prefix", "fragments"), [
    ({"idle": 5, "flush": 30}, "w.yml:10:9: warning: [user.config.invalid] ",
     ["tables.sql[0] window: idle_close_seconds is 5 and pipeline.flush_interval_seconds is 30"]),
    ({"event_time": False}, "w.yml:22:5: warning: [user.config.invalid] ",
     ["pipeline.source.kafka: a windowing pipeline whose source declares no event_time"]),
])
def test_window_warnings_keep_the_config_valid(scratch, options, prefix, fragments):
    result = validate(scratch, "w.yml", window_config(**options), env={})
    assert result.returncode == 0
    assert_report(result.stdout, [prefix + fragment for fragment in fragments] + ["w.yml: valid"])

    assert_config_valid(validate(scratch, "w.yml", window_config(**options), env={}, command=("config", "validate")), "w.yml")


def test_window_faults_sorted_with_json_report(scratch):
    text = window_config(coltype="TIMESTAMP", interval="30 minutes", emit="SELECT 1", lateness=60, idle=5)
    result = validate(scratch, "w.yml", text, env={})
    assert result.returncode == 10
    window = "w.yml:{}: [user.config.invalid] tables.sql[0] window: "
    assert_report(result.stdout, [
        window.format("7:9: error") + 'time_column "bucket" is not declared as TIMESTAMPTZ',
        window.format("7:9: error") + 'time_column "bucket" must be computed as',
        window.format("10:9: warning") + "idle_close_seconds is 5",
        window.format("11:9: error") + "allowed_lateness_seconds is 60",
        window.format("12:9: error") + "emit_sql does not read closed",
    ])

    report = json.loads(validate(scratch, "w.yml", text, "--json", env={}).stdout)
    assert report["ok"] is False
    summary = [(d["severity"], d["code"], d["class"], d["position"]["line"], d["position"]["column"]) for d in report["diagnostics"]]
    assert summary == [
        ("error", "user.config.invalid", "user", 7, 9), ("error", "user.config.invalid", "user", 7, 9),
        ("warning", "user.config.invalid", "user", 10, 9), ("error", "user.config.invalid", "user", 11, 9),
        ("error", "user.config.invalid", "user", 12, 9)]
