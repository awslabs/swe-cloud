"""import: the written configuration document, appending, refusals and error effects."""

import datetime

import pytest
from conftest import (
    LOGGING_OFF,
    ROLE,
    SCHEDULER_ROLE,
    assert_json,
    assert_report,
    tag_counter,
)

PRETTY_DEFINITION = '{\n  "States": {"Pause": {"Type": "Wait", "Seconds": 5, "Next": "Done"},\n  "Done": {"Type": "Succeed"}},\n  "StartAt": "Pause"\n}'
DEFINITION = {"StartAt": "Pause", "States": {"Pause": {"Type": "Wait", "Seconds": 5, "Next": "Done"}, "Done": {"Type": "Succeed"}}}
NULL_TARGET_FIELDS = {"ecsParameters": None, "eventBridgeParameters": None, "kinesisParameters": None, "sageMakerPipelineParameters": None, "sqsParameters": None}


def instant(text):
    return datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))


def assert_entry(actual, expected):
    """Compare an imported entry; tags are a multiset and timestamps are instants."""
    actual = dict(actual)
    assert isinstance(actual.get("state"), dict)
    actual_state, expected_state = dict(actual.pop("state")), dict(expected["state"])
    assert tag_counter(actual_state.pop("tags")) == tag_counter(expected_state.pop("tags"))
    assert_json(actual_state, expected_state)
    actual_schedule, expected_schedule = actual.pop("schedule", "absent"), expected["schedule"]
    if expected_schedule is None:
        assert actual_schedule is None
    else:
        actual_schedule, expected_schedule = dict(actual_schedule), dict(expected_schedule)
        for field in ("startDate", "endDate"):
            got, want = actual_schedule.pop(field, "absent"), expected_schedule.pop(field)
            assert (got is None and want is None) or (isinstance(got, str) and want is not None and instant(got) == instant(want)), (field, got)
        assert_json(actual_schedule, expected_schedule)
    assert_json(actual, {"deleteAll": False, "deleteSchedule": False})


def make_machine(env, name, **extra):
    env.sfn.create_state_machine(name=name, definition=PRETTY_DEFINITION, roleArn=ROLE, **extra)


def test_import_machine_and_schedule_into_new_file(env):
    group = env.group()
    machine = env.name("imp")
    sched = env.schedule_name("imp-sch", group)
    log_group = f"arn:aws:logs:us-east-1:{env.account}:log-group:imp:*"
    make_machine(env, machine, type="EXPRESS", tags=[{"key": "Team", "value": "data"}, {"key": "Tier", "value": "2"}],
                 loggingConfiguration={"level": "ERROR", "includeExecutionData": True, "destinations": [{"cloudWatchLogsLogGroup": {"logGroupArn": log_group}}]},
                 tracingConfiguration={"enabled": True})
    env.scheduler.create_schedule(
        GroupName=group, Name=sched, Description="imported", State="DISABLED", ScheduleExpression="cron(15 4 * * ? *)",
        ScheduleExpressionTimezone="UTC", StartDate=datetime.datetime(2030, 2, 1, tzinfo=datetime.timezone.utc),
        FlexibleTimeWindow={"Mode": "FLEXIBLE", "MaximumWindowInMinutes": 30},
        Target={"Arn": env.arn(machine), "RoleArn": SCHEDULER_ROLE, "Input": '{"a": [1, 2]}',
                "RetryPolicy": {"MaximumEventAgeInSeconds": 120, "MaximumRetryAttempts": 4},
                "DeadLetterConfig": {"Arn": f"arn:aws:sqs:us-east-1:{env.account}:dlq"}},
    )
    proc = env.run("import", "-c", "imported.jsonnet", "--sfn-name", machine, "--scheduler-name-with-group", f"{group}/{sched}")
    assert proc.returncode == 0, proc.stderr
    document = env.read_json("imported.jsonnet")
    assert isinstance(document, list) and len(document) == 1
    assert_entry(document[0], {
        "state": {
            "name": machine, "definition": DEFINITION, "roleArn": ROLE, "type": "EXPRESS",
            "loggingConfiguration": {"level": "ERROR", "includeExecutionData": True, "destinations": [{"cloudWatchLogsLogGroup": {"logGroupArn": log_group}}]},
            "tracingConfiguration": {"enabled": True},
            "tags": [{"key": "Team", "value": "data"}, {"key": "Tier", "value": "2"}],
        },
        "schedule": {
            "groupName": group, "name": sched, "description": "imported", "state": "DISABLED", "scheduleExpression": "cron(15 4 * * ? *)",
            "scheduleExpressionTimezone": "UTC", "startDate": "2030-02-01T00:00:00Z", "endDate": None,
            "flexibleTimeWindow": {"mode": "FLEXIBLE", "maximumWindowInMinutes": 30}, "kmsKeyArn": None,
            "target": {"arn": env.arn(machine), "roleArn": SCHEDULER_ROLE, "input": '{"a": [1, 2]}',
                       "retryPolicy": {"maximumEventAgeInSeconds": 120, "maximumRetryAttempts": 4},
                       "deadLetterConfig": {"arn": f"arn:aws:sqs:us-east-1:{env.account}:dlq"}, **NULL_TARGET_FIELDS},
        },
    })
    # import only reads AWS
    assert env.machine(machine)["type"] == "EXPRESS"
    assert env.schedule(group, sched)["Description"] == "imported"


def test_import_without_schedule_or_with_missing_schedule(env):
    group = env.group()
    machine = env.name("solo")
    make_machine(env, machine)
    expected = {
        "state": {"name": machine, "definition": DEFINITION, "roleArn": ROLE, "type": "STANDARD",
                  "loggingConfiguration": LOGGING_OFF, "tracingConfiguration": {"enabled": False}, "tags": []},
        "schedule": None,
    }
    for filename, extra in (("a.jsonnet", []), ("b.jsonnet", ["-s", f"{group}/absent-{env.suffix}"])):
        proc = env.run("import", "-c", filename, "-f", machine, *extra)
        assert proc.returncode == 0, proc.stderr
        document = env.read_json(filename)
        assert isinstance(document, list) and len(document) == 1
        assert_entry(document[0], expected)


def test_import_appends_to_evaluated_existing_config(env):
    machine = env.name("appended")
    existing = env.name("existing")
    make_machine(env, machine, tags=[{"key": "k", "value": "v"}])
    env.write("jobs.jsonnet", f"""
local owner = std.extVar('owner');
local job(name) = {{
  state: {{ name: name, type: 'EXPRESS', roleArn: 'arn:aws:iam::123456789012:role/' + owner,
           definition: {{ StartAt: 'S', States: {{ S: {{ Type: 'Succeed' }} }} }}, tags: [{{ key: 'owner', value: owner }}] }},
}};
[
  job('{existing}'),
  job('second-{env.suffix}') + {{
    schedule: {{ name: 'second-sch', state: 'ENABLED', scheduleExpression: 'rate(2 hours)', flexibleTimeWindow: {{ mode: 'OFF' }},
                target: {{ arn: 'arn:aws:states:us-east-1:{env.account}:stateMachine:second-{env.suffix}', roleArn: '{SCHEDULER_ROLE}' }} }},
    deleteSchedule: true,
  }},
]
""")
    before = (env.root / "jobs.jsonnet").read_bytes()
    proc = env.run("import", "-c", "jobs.jsonnet", "-f", machine)
    assert proc.returncode != 0, "the existing config needs its external variable"
    assert (env.root / "jobs.jsonnet").read_bytes() == before

    proc = env.run("import", "-c", "jobs.jsonnet", "-V", "owner=ana", "-f", machine)
    assert proc.returncode == 0, proc.stderr
    document = env.read_json("jobs.jsonnet")
    assert isinstance(document, list) and len(document) == 3
    simple = {"StartAt": "S", "States": {"S": {"Type": "Succeed"}}}
    first = {"name": existing, "definition": simple, "roleArn": "arn:aws:iam::123456789012:role/ana", "type": "EXPRESS",
             "loggingConfiguration": None, "tracingConfiguration": None, "tags": [{"key": "owner", "value": "ana"}]}
    assert_entry(document[0], {"state": first, "schedule": None})
    second = document[1]
    assert second.get("deleteSchedule") is True and second.get("deleteAll") is False
    assert_json(second["schedule"], {
        "groupName": "default", "name": "second-sch", "description": None, "state": "ENABLED", "scheduleExpression": "rate(2 hours)",
        "scheduleExpressionTimezone": None, "startDate": None, "endDate": None, "flexibleTimeWindow": {"mode": "OFF", "maximumWindowInMinutes": None},
        "kmsKeyArn": None,
        "target": {"arn": f"arn:aws:states:us-east-1:{env.account}:stateMachine:second-{env.suffix}", "roleArn": SCHEDULER_ROLE,
                   "deadLetterConfig": None, "input": None, "retryPolicy": None, **NULL_TARGET_FIELDS},
    })
    assert second["state"]["name"] == f"second-{env.suffix}"
    assert_entry(document[2], {
        "state": {"name": machine, "definition": DEFINITION, "roleArn": ROLE, "type": "STANDARD",
                  "loggingConfiguration": LOGGING_OFF, "tracingConfiguration": {"enabled": False}, "tags": [{"key": "k", "value": "v"}]},
        "schedule": None,
    })


def test_import_refuses_name_already_in_config(env):
    machine = env.name("dup")
    make_machine(env, machine)
    assert env.run("import", "-c", "c.jsonnet", "-f", machine).returncode == 0
    before = (env.root / "c.jsonnet").read_bytes()
    proc = env.run("import", "-c", "c.jsonnet", "-f", machine)
    assert proc.returncode != 0 and proc.stderr.strip()
    assert (env.root / "c.jsonnet").read_bytes() == before

    # a hand-written entry with that name is refused too, even if it differs
    env.write("hand.jsonnet", [{"state": {"name": machine, "type": "STANDARD", "roleArn": "r", "definition": {}, "tags": []}}])
    before = (env.root / "hand.jsonnet").read_bytes()
    proc = env.run("import", "-c", "hand.jsonnet", "-f", machine)
    assert proc.returncode != 0
    assert (env.root / "hand.jsonnet").read_bytes() == before


@pytest.mark.parametrize("case", ["missing-machine", "malformed-schedule-reference"])
def test_import_errors_write_nothing(env, case):
    group = env.group()
    machine = env.name("err")
    if case == "missing-machine":
        args = ["-f", machine, "-s", f"{group}/whatever"]
    else:
        make_machine(env, machine)
        args = ["-f", machine, "-s", "no-slash-here"]
    proc = env.run("import", "-c", "new.jsonnet", *args)
    assert proc.returncode != 0 and proc.stderr.strip()
    assert not (env.root / "new.jsonnet").exists()

    env.write("old.jsonnet", "[]")
    proc = env.run("import", "-c", "old.jsonnet", *args)
    assert proc.returncode != 0
    assert (env.root / "old.jsonnet").read_text() == "[]"


def test_import_default_path_and_plan_round_trip(env):
    group = env.group()
    machine = env.name("rt")
    sched = env.schedule_name("rt-sch", group)
    make_machine(env, machine, tags=[{"key": "a", "value": "1"}, {"key": "b", "value": "2"}])
    env.scheduler.create_schedule(GroupName=group, Name=sched, State="ENABLED", ScheduleExpression="rate(3 hours)",
                                  FlexibleTimeWindow={"Mode": "OFF"}, Target={"Arn": env.arn(machine), "RoleArn": SCHEDULER_ROLE, "Input": "{}"})
    proc = env.run("import", "-f", machine, "-s", f"{group}/{sched}")
    assert proc.returncode == 0, proc.stderr
    assert (env.root / "fubura.jsonnet").exists()
    proc = env.run("plan", "-o", "rt.json")
    assert proc.returncode == 0, proc.stderr
    assert_report(env.read_json("rt.json"), [])
