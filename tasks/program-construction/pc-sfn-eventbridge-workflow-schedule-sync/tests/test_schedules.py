"""Schedule operations, the deletion flags and failure effects."""

import datetime

from conftest import (
    SCHEDULER_ROLE,
    assert_report,
    assert_stdout,
    changes,
    diff_lines,
    output_lines,
    schedule,
    state,
    wait_definition,
)


def notice_lines(text):
    return [line for line in text.split("\n") if line]


def deployed(env, label="job", **schedule_fields):
    """A state machine plus schedule created through the program; returns (group, machine, schedule, entry)."""
    group = env.group()
    machine = env.name(label)
    sched = env.schedule_name(label + "-sch", group)
    entry = {"state": state(machine, tags=[("Team", "ops")]), "schedule": schedule(env, group, sched, machine, **schedule_fields)}
    env.write("base.jsonnet", [entry])
    env.report("apply", "base.jsonnet", "-a")
    assert env.machine(machine) is not None and env.schedule(group, sched) is not None
    return group, machine, sched, entry


def test_apply_creates_schedule_with_all_fields(env):
    group = env.group()
    machine = env.name("sched")
    sched = env.schedule_name("sched-sch", group)
    fields = {
        "description": "every morning",
        "state": "DISABLED",
        "scheduleExpression": "cron(30 6 * * ? *)",
        "scheduleExpressionTimezone": "Europe/Paris",
        "startDate": "2030-01-01T00:00:00Z",
        "endDate": "2031-06-30T12:00:00Z",
        "flexibleTimeWindow": {"mode": "FLEXIBLE", "maximumWindowInMinutes": 20},
        "target": {
            "arn": env.arn(machine),
            "roleArn": SCHEDULER_ROLE,
            "input": '{"mode": "full"}',
            "retryPolicy": {"maximumEventAgeInSeconds": 600, "maximumRetryAttempts": 2},
            "deadLetterConfig": {"arn": f"arn:aws:sqs:us-east-1:{env.account}:dead"},
        },
    }
    env.write("c.jsonnet", [{"state": state(machine), "schedule": schedule(env, group, sched, machine, **fields)}])
    proc, report = env.report("apply", "c.jsonnet", "-a")
    assert_report(report, [(machine, ["create_state", "create_schedule"])])

    found = env.schedule(group, sched)
    utc = datetime.timezone.utc
    assert found["Arn"].endswith(f"schedule/{group}/{sched}")
    expected = {
        "GroupName": group,
        "Name": sched,
        "Description": "every morning",
        "State": "DISABLED",
        "ScheduleExpression": "cron(30 6 * * ? *)",
        "ScheduleExpressionTimezone": "Europe/Paris",
        "StartDate": datetime.datetime(2030, 1, 1, tzinfo=utc),
        "EndDate": datetime.datetime(2031, 6, 30, 12, tzinfo=utc),
        "FlexibleTimeWindow": {"Mode": "FLEXIBLE", "MaximumWindowInMinutes": 20},
        "Target": {
            "Arn": env.arn(machine),
            "RoleArn": SCHEDULER_ROLE,
            "Input": '{"mode": "full"}',
            "RetryPolicy": {"MaximumEventAgeInSeconds": 600, "MaximumRetryAttempts": 2},
            "DeadLetterConfig": {"Arn": f"arn:aws:sqs:us-east-1:{env.account}:dead"},
        },
    }
    assert {key: found.get(key) for key in expected} == expected
    assert "KmsKeyArn" not in found
    assert env.machine(machine) is not None

    proc, report = env.report("plan", "c.jsonnet")
    assert_report(report, [])
    assert_stdout(proc, report, unchanged_entries=1)


def test_schedule_without_group_goes_to_default_group(env):
    machine = env.name("dflt")
    sched = env.schedule_name("dflt-sch", "default")
    entry = schedule(env, "default", sched, machine)
    del entry["groupName"]
    env.write("c.jsonnet", [{"state": state(machine), "schedule": entry}])
    env.report("apply", "c.jsonnet", "-a")
    found = env.schedule("default", sched)
    assert found is not None and found["GroupName"] == "default" and found["Target"]["Arn"] == env.arn(machine)
    _, report = env.report("plan", "c.jsonnet")
    assert_report(report, [])


def test_update_schedule_only(env):
    group, machine, sched, entry = deployed(env)
    machine_before = env.machine(machine)
    changed = {**entry, "schedule": {**entry["schedule"], "state": "DISABLED", "scheduleExpression": "rate(15 minutes)",
                                     "target": {**entry["schedule"]["target"], "input": '{"v": 2}'}}}
    env.write("v2.jsonnet", [changed])
    proc, report = env.report("plan", "v2.jsonnet")
    assert_report(report, [(machine, ["update_schedule"])])
    assert_stdout(proc, report, unchanged_entries=0)
    headers, removed, added = diff_lines(report["text_diff"][0])
    assert headers == [(sched, sched)]
    assert (removed, added) == changes(
        ("-", '  "state": "ENABLED",'),
        ("+", '  "state": "DISABLED",'),
        ("-", '  "scheduleExpression": "rate(1 hour)",'),
        ("+", '  "scheduleExpression": "rate(15 minutes)",'),
        ("-", '    "input": "{}",'),
        ("+", '    "input": "{\\"v\\": 2}",'),
    )
    assert env.schedule(group, sched)["ScheduleExpression"] == "rate(1 hour)"

    env.report("apply", "v2.jsonnet", "-a")
    env.assert_schedule(group, sched, {
        "GroupName": group, "Name": sched, "State": "DISABLED", "ScheduleExpression": "rate(15 minutes)",
        "FlexibleTimeWindow": {"Mode": "OFF"},
        "Target": {"Arn": env.arn(machine), "RoleArn": SCHEDULER_ROLE, "Input": '{"v": 2}'},
    })
    assert env.machine(machine) == machine_before
    _, report = env.report("plan", "v2.jsonnet")
    assert_report(report, [])


FULL_SCHEDULE_FIELDS = {
    "description": "full schedule",
    "state": "ENABLED",
    "scheduleExpression": "cron(0 5 * * ? *)",
    "scheduleExpressionTimezone": "Europe/Paris",
    "startDate": "2030-03-01T00:00:00Z",
    "endDate": "2032-03-01T00:00:00Z",
    "flexibleTimeWindow": {"mode": "FLEXIBLE", "maximumWindowInMinutes": 10},
}


def test_update_fully_populated_schedule_keeps_and_changes_every_field(env):
    group = env.group()
    machine = env.name("fullupd")
    sched = env.schedule_name("fullupd-sch", group)
    target = {"arn": env.arn(machine), "roleArn": SCHEDULER_ROLE, "input": '{"run": 1}',
              "retryPolicy": {"maximumEventAgeInSeconds": 600, "maximumRetryAttempts": 2},
              "deadLetterConfig": {"arn": f"arn:aws:sqs:us-east-1:{env.account}:dlq-a"}}
    env.write("v1.jsonnet", [{"state": state(machine), "schedule": schedule(env, group, sched, machine, **FULL_SCHEDULE_FIELDS, target=target)}])
    env.report("apply", "v1.jsonnet", "-a")
    utc = datetime.timezone.utc
    expected = {
        "GroupName": group, "Name": sched, "Description": "full schedule", "State": "ENABLED",
        "ScheduleExpression": "cron(0 5 * * ? *)", "ScheduleExpressionTimezone": "Europe/Paris",
        "StartDate": datetime.datetime(2030, 3, 1, tzinfo=utc), "EndDate": datetime.datetime(2032, 3, 1, tzinfo=utc),
        "FlexibleTimeWindow": {"Mode": "FLEXIBLE", "MaximumWindowInMinutes": 10},
        "Target": {"Arn": env.arn(machine), "RoleArn": SCHEDULER_ROLE, "Input": '{"run": 1}',
                   "RetryPolicy": {"MaximumEventAgeInSeconds": 600, "MaximumRetryAttempts": 2},
                   "DeadLetterConfig": {"Arn": f"arn:aws:sqs:us-east-1:{env.account}:dlq-a"}},
    }
    env.assert_schedule(group, sched, expected)
    machine_before = env.machine(machine)

    # 1. a change to an unrelated field must carry every other field through the update
    env.write("v2.jsonnet", [{"state": state(machine), "schedule": schedule(env, group, sched, machine, **{**FULL_SCHEDULE_FIELDS, "state": "DISABLED"}, target=target)}])
    proc, report = env.report("apply", "v2.jsonnet", "-a")
    assert_report(report, [(machine, ["update_schedule"])])
    _, removed, added = diff_lines(report["text_diff"][0])
    assert (removed, added) == changes(("-", '  "state": "ENABLED",'), ("+", '  "state": "DISABLED",'))
    expected["State"] = "DISABLED"
    env.assert_schedule(group, sched, expected)

    # 2. change timezone, end date, window, retry policy and dead-letter target together
    fields = {**FULL_SCHEDULE_FIELDS, "state": "DISABLED", "scheduleExpressionTimezone": "UTC", "endDate": "2033-01-01T00:00:00Z",
              "flexibleTimeWindow": {"mode": "FLEXIBLE", "maximumWindowInMinutes": 45}}
    target3 = {**target, "retryPolicy": {"maximumEventAgeInSeconds": 900, "maximumRetryAttempts": 5},
               "deadLetterConfig": {"arn": f"arn:aws:sqs:us-east-1:{env.account}:dlq-b"}}
    env.write("v3.jsonnet", [{"state": state(machine), "schedule": schedule(env, group, sched, machine, **fields, target=target3)}])
    proc, report = env.report("plan", "v3.jsonnet")
    assert_report(report, [(machine, ["update_schedule"])])
    assert_stdout(proc, report, unchanged_entries=0)
    headers, removed, added = diff_lines(report["text_diff"][0])
    assert headers == [(sched, sched)]
    assert (removed, added) == changes(
        ("-", '  "scheduleExpressionTimezone": "Europe/Paris",'), ("+", '  "scheduleExpressionTimezone": "UTC",'),
        ("-", '  "endDate": "2032-03-01T00:00:00Z",'), ("+", '  "endDate": "2033-01-01T00:00:00Z",'),
        ("-", '    "maximumWindowInMinutes": 10'), ("+", '    "maximumWindowInMinutes": 45'),
        ("-", f'      "arn": "arn:aws:sqs:us-east-1:{env.account}:dlq-a"'), ("+", f'      "arn": "arn:aws:sqs:us-east-1:{env.account}:dlq-b"'),
        ("-", '      "maximumEventAgeInSeconds": 600,'), ("+", '      "maximumEventAgeInSeconds": 900,'),
        ("-", '      "maximumRetryAttempts": 2'), ("+", '      "maximumRetryAttempts": 5'),
    )
    env.assert_schedule(group, sched, expected)

    env.report("apply", "v3.jsonnet", "-a")
    expected.update({"ScheduleExpressionTimezone": "UTC", "EndDate": datetime.datetime(2033, 1, 1, tzinfo=utc),
                     "FlexibleTimeWindow": {"Mode": "FLEXIBLE", "MaximumWindowInMinutes": 45}})
    expected["Target"] = {**expected["Target"], "RetryPolicy": {"MaximumEventAgeInSeconds": 900, "MaximumRetryAttempts": 5},
                          "DeadLetterConfig": {"Arn": f"arn:aws:sqs:us-east-1:{env.account}:dlq-b"}}
    env.assert_schedule(group, sched, expected)
    assert env.machine(machine) == machine_before
    proc, report = env.report("plan", "v3.jsonnet")
    assert_report(report, [])
    assert_stdout(proc, report, unchanged_entries=1)


def test_state_and_schedule_change_together(env):
    group, machine, sched, entry = deployed(env)
    changed = {"state": state(machine, definition=wait_definition(4), tags=[("Team", "ops")]),
               "schedule": {**entry["schedule"], "description": "described"}}
    env.write("v2.jsonnet", [changed])
    proc, report = env.report("apply", "v2.jsonnet", "-a")
    assert_report(report, [(machine, ["update_state", "update_schedule"])])
    headers, removed, added = diff_lines(report["text_diff"][0])
    assert headers == [(machine, machine), (sched, sched)]
    assert added['+  "description": "described",'] == 1 and removed['-  "description": null,'] == 1
    assert env.machine(machine)["definition"] == wait_definition(4)
    assert env.schedule(group, sched)["Description"] == "described"


def test_schedule_left_out_of_config_is_untouched(env):
    group, machine, sched, entry = deployed(env)
    schedule_before = env.schedule(group, sched)
    for variant in ({"state": entry["state"]}, {"state": entry["state"], "schedule": None}):
        env.write("v2.jsonnet", [variant])
        proc, report = env.report("apply", "v2.jsonnet", "-a")
        assert_report(report, [])
        assert_stdout(proc, report, unchanged_entries=1)
    assert env.schedule(group, sched) == schedule_before


def test_delete_schedule_flag(env):
    group, machine, sched, entry = deployed(env)
    sentinel = env.schedule_name("sentinel", group)
    env.scheduler.create_schedule(GroupName=group, Name=sentinel, ScheduleExpression="rate(1 day)", State="DISABLED",
                                  FlexibleTimeWindow={"Mode": "OFF"}, Target={"Arn": env.arn(machine), "RoleArn": SCHEDULER_ROLE})
    machine_before = env.machine(machine)
    env.write("v2.jsonnet", [{**entry, "deleteSchedule": True}])

    proc, report = env.report("plan", "v2.jsonnet")
    assert_report(report, [(machine, ["delete_schedule"])])
    assert notice_lines(report["text_diff"][0]) == [f"Schedule({sched}) is going to be deleted"]
    assert_stdout(proc, report, unchanged_entries=0)
    assert env.schedule(group, sched) is not None

    env.report("apply", "v2.jsonnet", "-a")
    assert env.schedule(group, sched) is None
    assert env.schedule(group, sentinel) is not None
    assert env.machine(machine) == machine_before

    proc, report = env.report("apply", "v2.jsonnet", "-a")
    assert_report(report, [])
    assert_stdout(proc, report, unchanged_entries=1)


def test_delete_schedule_flag_with_state_change(env):
    group, machine, sched, entry = deployed(env)
    env.write("v2.jsonnet", [{"state": state(machine, definition=wait_definition(9), tags=[("Team", "ops")]), "schedule": entry["schedule"], "deleteSchedule": True}])
    proc, report = env.report("apply", "v2.jsonnet", "-a")
    assert_report(report, [(machine, ["update_state", "delete_schedule"])])
    text = report["text_diff"][0]
    headers, removed, added = diff_lines(text)
    assert headers == [(machine, machine)]
    assert notice_lines(text)[-1] == f"Schedule({sched}) is going to be deleted"
    assert env.schedule(group, sched) is None
    assert env.machine(machine)["definition"] == wait_definition(9)


def test_delete_all_flag(env):
    group, machine, sched, entry = deployed(env)
    sentinel = env.schedule_name("sentinel", group)
    env.scheduler.create_schedule(GroupName=group, Name=sentinel, ScheduleExpression="rate(1 day)", State="DISABLED",
                                  FlexibleTimeWindow={"Mode": "OFF"}, Target={"Arn": env.arn(machine), "RoleArn": SCHEDULER_ROLE})
    # a changed definition does not matter once deleteAll is set
    env.write("v2.jsonnet", [{"state": state(machine, definition=wait_definition(1)), "schedule": entry["schedule"], "deleteAll": True}])

    proc, report = env.report("plan", "v2.jsonnet")
    assert_report(report, [(machine, ["delete_schedule", "delete_state"])])
    assert notice_lines(report["text_diff"][0]) == [f"State machine({machine}) is going to be deleted", f"Schedule({sched}) is going to be deleted"]
    assert_stdout(proc, report, unchanged_entries=0)
    assert env.machine(machine) is not None

    env.report("apply", "v2.jsonnet", "-a")
    assert env.machine(machine) is None
    assert env.schedule(group, sched) is None
    assert env.schedule(group, sentinel) is not None

    proc, report = env.report("plan", "v2.jsonnet")
    assert_report(report, [])
    assert_stdout(proc, report, unchanged_entries=1)


def test_delete_all_without_schedule_block_keeps_remote_schedule(env):
    group, machine, sched, entry = deployed(env)
    schedule_before = env.schedule(group, sched)
    env.write("v2.jsonnet", [{"state": entry["state"], "deleteAll": True}])
    proc, report = env.report("apply", "v2.jsonnet", "-a")
    assert_report(report, [(machine, ["delete_state"])])
    assert notice_lines(report["text_diff"][0]) == [f"State machine({machine}) is going to be deleted"]
    assert env.machine(machine) is None
    assert env.schedule(group, sched) == schedule_before


def test_delete_all_when_only_schedule_exists(env):
    group = env.group()
    machine = env.name("orphan")
    sched = env.schedule_name("orphan-sch", group)
    env.scheduler.create_schedule(GroupName=group, Name=sched, ScheduleExpression="rate(1 day)", State="ENABLED",
                                  FlexibleTimeWindow={"Mode": "OFF"}, Target={"Arn": env.arn(machine), "RoleArn": SCHEDULER_ROLE})
    env.write("c.jsonnet", [{"state": state(machine), "schedule": schedule(env, group, sched, machine), "deleteAll": True}])
    proc, report = env.report("apply", "c.jsonnet", "-a")
    assert_report(report, [(machine, ["delete_schedule"])])
    assert notice_lines(report["text_diff"][0]) == [f"Schedule({sched}) is going to be deleted"]
    assert env.schedule(group, sched) is None
    assert env.describe(machine) is None


def test_deletion_flags_with_nothing_remote_are_no_change(env):
    group = env.group()
    machine = env.name("ghost")
    sched = env.schedule_name("ghost-sch", group)
    entries = [
        {"state": state(machine), "schedule": schedule(env, group, sched, machine), "deleteAll": True},
        {"state": state(machine), "deleteAll": True},
    ]
    for entry in entries:
        env.write("c.jsonnet", [entry])
        proc, report = env.report("apply", "c.jsonnet", "-a")
        assert_report(report, [])
        assert_stdout(proc, report, unchanged_entries=1)
    # deleteSchedule on a machine without a remote schedule only creates the machine
    env.write("c.jsonnet", [{"state": state(machine), "schedule": schedule(env, group, sched, machine), "deleteSchedule": True}])
    _, report = env.report("apply", "c.jsonnet", "-a")
    assert_report(report, [(machine, ["create_state"])])
    assert env.machine(machine) is not None and env.schedule(group, sched) is None


def test_failed_schedule_creation_keeps_created_machine(env):
    machine = env.name("partial")
    missing_group = "missing-" + env.suffix
    sched = env.schedule_name("partial-sch", missing_group)
    env.write("c.jsonnet", [{"state": state(machine), "schedule": schedule(env, missing_group, sched, machine)}])

    proc, report = env.report("plan", "c.jsonnet")
    assert_report(report, [(machine, ["create_state", "create_schedule"])])

    proc = env.run("apply", "-a", "-c", "c.jsonnet")
    assert proc.returncode != 0 and proc.stderr.strip()
    assert "Fubura will:" in output_lines(proc.stdout)
    assert env.machine(machine) is not None

    proc, report = env.report("plan", "c.jsonnet")
    assert_report(report, [(machine, ["create_schedule"])])
    headers, _, _ = diff_lines(report["text_diff"][0])
    assert headers == [(sched, sched)]
