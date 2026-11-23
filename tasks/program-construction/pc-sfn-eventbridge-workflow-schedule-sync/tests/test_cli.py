"""Invocation, Jsonnet evaluation, confirmation and the plan/report serialization."""

import pytest
from conftest import (
    LOGGING_OFF,
    ROLE,
    SCHEDULER_ROLE,
    added_block,
    assert_report,
    assert_stdout,
    diff_lines,
    output_lines,
    pass_definition,
    schedule,
    state,
)


def test_plan_create_renders_both_local_documents(env):
    group = env.group()
    machine = env.name("render")
    sched = env.schedule_name("render-sch", group)
    config = f"""
local name = '{machine}';
local logGroup = 'arn:aws:logs:us-east-1:{env.account}:log-group:render:*';
[{{
  state: {{
    name: name,
    type: 'EXPRESS',
    roleArn: '{ROLE}',
    definition: {{
      StartAt: 'Prepare',
      States: {{
        Prepare: {{ Type: 'Pass', Result: {{ zeta: true, alpha: [3, 1, 2], mid: 'x' }}, Next: 'Done' }},
        Done: {{ Type: 'Succeed' }},
      }},
      Comment: 'render',
    }},
    loggingConfiguration: {{ level: 'ALL', includeExecutionData: true, destinations: [{{ cloudWatchLogsLogGroup: {{ logGroupArn: logGroup }} }}] }},
    tracingConfiguration: {{ enabled: true }},
    tags: [{{ key: 'Team', value: 'batch' }}, {{ key: 'Env', value: 'test' }}],
  }},
  schedule: {{
    groupName: '{group}',
    name: '{sched}',
    description: 'nightly',
    state: 'DISABLED',
    scheduleExpression: 'cron(0 2 * * ? *)',
    scheduleExpressionTimezone: 'Asia/Tokyo',
    startDate: '2030-01-01T00:00:00Z',
    flexibleTimeWindow: {{ mode: 'FLEXIBLE', maximumWindowInMinutes: 15 }},
    target: {{
      arn: '{env.arn(machine)}',
      roleArn: '{SCHEDULER_ROLE}',
      input: '{{"from": "schedule"}}',
      deadLetterConfig: {{ arn: 'arn:aws:sqs:us-east-1:{env.account}:dlq' }},
      retryPolicy: {{ maximumEventAgeInSeconds: 3600, maximumRetryAttempts: 3 }},
    }},
  }},
}}]
"""
    env.write("render.jsonnet", config)
    proc, report = env.report("plan", "render.jsonnet")
    assert_report(report, [(machine, ["create_state", "create_schedule"])])
    assert_stdout(proc, report, unchanged_entries=0)

    headers, removed, added = diff_lines(report["text_diff"][0])
    assert headers == [(machine, machine), (sched, sched)]
    assert removed == {"-null": 2}
    expected = added_block(f"""
{{
  "name": "{machine}",
  "definition": {{
    "Comment": "render",
    "StartAt": "Prepare",
    "States": {{
      "Done": {{
        "Type": "Succeed"
      }},
      "Prepare": {{
        "Next": "Done",
        "Result": {{
          "alpha": [
            3,
            1,
            2
          ],
          "mid": "x",
          "zeta": true
        }},
        "Type": "Pass"
      }}
    }}
  }},
  "roleArn": "{ROLE}",
  "type": "EXPRESS",
  "loggingConfiguration": {{
    "level": "ALL",
    "includeExecutionData": true,
    "destinations": [
      {{
        "cloudWatchLogsLogGroup": {{
          "logGroupArn": "arn:aws:logs:us-east-1:{env.account}:log-group:render:*"
        }}
      }}
    ]
  }},
  "tracingConfiguration": {{
    "enabled": true
  }},
  "tags": [
    {{
      "key": "Team",
      "value": "batch"
    }},
    {{
      "key": "Env",
      "value": "test"
    }}
  ]
}}
""") + added_block(f"""
{{
  "groupName": "{group}",
  "name": "{sched}",
  "description": "nightly",
  "state": "DISABLED",
  "scheduleExpression": "cron(0 2 * * ? *)",
  "scheduleExpressionTimezone": "Asia/Tokyo",
  "startDate": "2030-01-01T00:00:00Z",
  "endDate": null,
  "flexibleTimeWindow": {{
    "mode": "FLEXIBLE",
    "maximumWindowInMinutes": 15
  }},
  "kmsKeyArn": null,
  "target": {{
    "arn": "{env.arn(machine)}",
    "roleArn": "{SCHEDULER_ROLE}",
    "deadLetterConfig": {{
      "arn": "arn:aws:sqs:us-east-1:{env.account}:dlq"
    }},
    "ecsParameters": null,
    "eventBridgeParameters": null,
    "input": "{{\\"from\\": \\"schedule\\"}}",
    "kinesisParameters": null,
    "retryPolicy": {{
      "maximumEventAgeInSeconds": 3600,
      "maximumRetryAttempts": 3
    }},
    "sageMakerPipelineParameters": null,
    "sqsParameters": null
  }}
}}
""")
    assert added == expected
    # plan only reads
    assert env.describe(machine) is None
    assert env.schedule(group, sched) is None


def test_plan_renders_absent_optional_fields_as_null(env):
    bare = env.name("bare")
    other = env.name("dflt")
    sched = env.schedule_name("dflt-sch", "default")
    config = [
        {"state": state(bare, logging=None, tracing=None)},
        {
            "state": state(other, definition={"StartAt": "S", "States": {"S": {"Type": "Succeed"}}}, tags=[("k", "v")]),
            "schedule": {"name": sched, "state": "ENABLED", "scheduleExpression": "rate(5 minutes)", "flexibleTimeWindow": {"mode": "OFF"},
                         "target": {"arn": env.arn(other), "roleArn": SCHEDULER_ROLE}},
            "deleteAll": False,
        },
    ]
    env.write("bare.jsonnet", config)
    proc, report = env.report("plan", "bare.jsonnet")
    assert_report(report, [(bare, ["create_state"]), (other, ["create_state", "create_schedule"])])
    assert_stdout(proc, report, unchanged_entries=0)

    headers, removed, added = diff_lines(report["text_diff"][0])
    assert headers == [(bare, bare)] and removed == {"-null": 1}
    assert added == added_block(f"""
{{
  "name": "{bare}",
  "definition": {{
    "StartAt": "Prepare",
    "States": {{
      "Done": {{
        "Type": "Succeed"
      }},
      "Prepare": {{
        "Next": "Done",
        "Result": {{
          "n": 1
        }},
        "Type": "Pass"
      }}
    }}
  }},
  "roleArn": "{ROLE}",
  "type": "STANDARD",
  "loggingConfiguration": null,
  "tracingConfiguration": null,
  "tags": []
}}
""")
    headers, removed, added = diff_lines(report["text_diff"][1])
    assert headers == [(other, other), (sched, sched)] and removed == {"-null": 2}
    assert added == added_block(f"""
{{
  "name": "{other}",
  "definition": {{
    "StartAt": "S",
    "States": {{
      "S": {{
        "Type": "Succeed"
      }}
    }}
  }},
  "roleArn": "{ROLE}",
  "type": "STANDARD",
  "loggingConfiguration": {{
    "level": "OFF",
    "includeExecutionData": false,
    "destinations": []
  }},
  "tracingConfiguration": {{
    "enabled": false
  }},
  "tags": [
    {{
      "key": "k",
      "value": "v"
    }}
  ]
}}
""") + added_block(f"""
{{
  "groupName": "default",
  "name": "{sched}",
  "description": null,
  "state": "ENABLED",
  "scheduleExpression": "rate(5 minutes)",
  "scheduleExpressionTimezone": null,
  "startDate": null,
  "endDate": null,
  "flexibleTimeWindow": {{
    "mode": "OFF",
    "maximumWindowInMinutes": null
  }},
  "kmsKeyArn": null,
  "target": {{
    "arn": "{env.arn(other)}",
    "roleArn": "{SCHEDULER_ROLE}",
    "deadLetterConfig": null,
    "ecsParameters": null,
    "eventBridgeParameters": null,
    "input": null,
    "kinesisParameters": null,
    "retryPolicy": null,
    "sageMakerPipelineParameters": null,
    "sqsParameters": null
  }}
}}
""")
    assert env.describe(bare) is None and env.describe(other) is None
    assert env.schedule("default", sched) is None


def test_empty_config_reports_no_change(env):
    env.write("empty.jsonnet", "local nothing = []; nothing")
    for command in ("plan", "apply"):
        proc, report = env.report(command, "empty.jsonnet")
        assert_report(report, [])
        assert report["text_diff"] == []
        assert_stdout(proc, report, unchanged_entries=0)


def test_ext_str_values_and_null_binding(env):
    machine = env.name("ext")
    env.write("ext.jsonnet", f"""
local suffix = std.extVar('suffix');
local pair = std.extVar('pair');
local unset = std.extVar('unset');
[{{
  state: {{
    name: 'ext-' + suffix,
    type: 'STANDARD',
    roleArn: '{ROLE}',
    definition: {{ StartAt: 'S', States: {{ S: {{ Type: 'Succeed' }} }} }},
    tags: [{{ key: 'pair', value: pair }}, {{ key: 'unset', value: if unset == null then 'was-null' else 'was-' + unset }}],
  }},
}}]
""")
    proc, report = env.report("plan", "ext.jsonnet", "-V", f"suffix={env.suffix}", "-V", "pair=a=b", "--ext-str", "unset")
    assert_report(report, [(machine, ["create_state"])])
    _, _, added = diff_lines(report["text_diff"][0])
    assert added['+      "value": "a=b"'] == 1
    assert added['+      "value": "was-null"'] == 1

    proc, report = env.report("plan", "ext.jsonnet", "-V", f"suffix={env.suffix}", "-V", "pair=", "-V", "unset=")
    _, _, added = diff_lines(report["text_diff"][0])
    assert added['+      "value": ""'] == 1
    assert added['+      "value": "was-"'] == 1


def test_duplicate_ext_var_is_rejected(env):
    machine = env.name("dup")
    env.write("dup.jsonnet", f"[{{state: {{name: std.extVar('n'), type: 'STANDARD', roleArn: 'r', definition: {{StartAt: 'S', States: {{S: {{Type: 'Succeed'}}}}}}, tags: []}}}}]")
    proc = env.run("apply", "-a", "-c", "dup.jsonnet", "-V", f"n={machine}", "-V", "n=other", "-o", "dup.json")
    assert proc.returncode != 0 and proc.stderr.strip()
    assert not (env.root / "dup.json").exists()
    assert env.describe(machine) is None


@pytest.mark.parametrize(
    "content",
    [
        None,  # file does not exist
        "{ entries: [] }",  # not an array
        "[{ state: { name: 'x', type: 'STANDARD', roleArn: 'r', tags: [] } }]",  # definition missing
        "[{ state: { name: 'x', type: 'STANDARD', roleArn: 'r', definition: {}, tags: [] },",  # syntax error
        "[{ state: { name: std.extVar('missing'), type: 'STANDARD', roleArn: 'r', definition: {}, tags: [] } }]",  # unbound variable
        "[{ state: { name: 'x', type: 'SOMETIMES', roleArn: 'r', definition: {}, tags: [] } }]",  # invalid enum value
    ],
    ids=["missing-file", "not-array", "missing-field", "syntax", "unbound-extvar", "bad-type"],
)
@pytest.mark.parametrize("command", ["plan", "apply"])
def test_invalid_config_fails_without_output(env, content, command):
    if content is not None:
        env.write("bad.jsonnet", content)
    args = [command, "-c", "bad.jsonnet", "-o", "bad.json"] + (["-a"] if command == "apply" else [])
    proc = env.run(*args)
    assert proc.returncode != 0
    assert proc.stderr.strip()
    assert not (env.root / "bad.json").exists()


def test_unknown_option_is_rejected(env):
    proc = env.run("plan", "--no-such-option")
    assert proc.returncode != 0 and proc.stderr.strip()


def test_default_config_path(env):
    machine = env.name("default-path")
    env.write("fubura.jsonnet", [{"state": state(machine)}])
    env.write("other.jsonnet", [])
    proc = env.run("plan", "-o", "default.json")
    assert proc.returncode == 0, proc.stderr
    assert_report(env.read_json("default.json"), [(machine, ["create_state"])])


def test_target_filter_selects_entries_by_state_name(env):
    a, b, c = env.name("ta"), env.name("tb"), env.name("tc")
    env.write("multi.jsonnet", [{"state": state(n)} for n in (a, b, c)])
    proc, report = env.report("plan", "multi.jsonnet", "-t", c, "--target", a, "-t", "absent-" + env.suffix)
    assert_report(report, [(a, ["create_state"]), (c, ["create_state"])])
    assert all(b not in text for text in report["text_diff"])
    assert_stdout(proc, report, unchanged_entries=0)

    proc, report = env.report("plan", "multi.jsonnet", "-t", "absent-" + env.suffix)
    assert_report(report, [])
    assert_stdout(proc, report, unchanged_entries=0)


@pytest.mark.parametrize("answer", ["", "no\n", "yes \n", "YES\n", "y\n", " yes\n"], ids=["eof", "no", "trailing-space", "upper", "y", "leading-space"])
def test_apply_cancelled_unless_exactly_yes(env, answer):
    group = env.group()
    machine = env.name("confirm")
    sched = env.schedule_name("confirm-sch", group)
    env.write("c.jsonnet", [{"state": state(machine), "schedule": schedule(env, group, sched, machine)}])
    proc, report = env.report("apply", "c.jsonnet", stdin=answer, fails=True)
    assert "apply cancelled!" in proc.stderr
    # the report is still written; nothing is created
    assert_report(report, [(machine, ["create_state", "create_schedule"])])
    assert env.describe(machine) is None
    assert env.schedule(group, sched) is None


@pytest.mark.parametrize("answer", ["yes\n", "yes"], ids=["newline", "no-newline"])
def test_apply_proceeds_on_yes(env, answer):
    machine = env.name("approve")
    env.write("c.jsonnet", [{"state": state(machine)}])
    proc, report = env.report("apply", "c.jsonnet", stdin=answer)
    assert_report(report, [(machine, ["create_state"])])
    assert env.machine(machine) is not None


def test_apply_without_changes_does_not_prompt(env):
    machine = env.name("quiet")
    env.write("c.jsonnet", [{"state": state(machine)}])
    env.report("apply", "c.jsonnet", "-a")
    # stdin is /dev/null: no confirmation may be needed when nothing changes
    proc, report = env.report("apply", "c.jsonnet")
    assert_report(report, [])
    assert_stdout(proc, report, unchanged_entries=1)


def test_invalid_entry_aborts_before_any_change(env):
    good = env.name("good")
    bad = env.name("bad")
    env.write("c.jsonnet", [{"state": state(good)}, {"state": state(bad), "deleteSchedule": True}])
    for command in (["plan"], ["apply", "-a"]):
        proc = env.run(*command, "-c", "c.jsonnet", "-o", "out.json")
        assert proc.returncode != 0 and proc.stderr.strip()
        assert not (env.root / "out.json").exists()
        assert env.describe(good) is None and env.describe(bad) is None
    assert "Fubura will:" not in output_lines(proc.stdout)
