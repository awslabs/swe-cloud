"""Scheduler schedules and schedule groups: apply operations and stored state."""

from support import (
    TRACKING_KEY,
    assert_diff,
    changed_lines,
    cli,
    create_group,
    group_tags,
    put_schedule,
    same,
    schedule_names,
    schedule_snapshot,
    scheduler,
    write,
)


# The context line a unified diff shows for the empty final line of both sides.
SP = " "


def apply(ws, conf, *flags):
    return cli("-conf", conf, *flags, "-auto-approve", "apply", cwd=ws)


def test_create_schedules_groups_and_group_tags(ws, ns):
    main, side, pre = f"{ns.p}-main", f"{ns.p}-side", f"{ns.p}-pre"
    create_group(pre, {"Old": "1"})
    write(
        ws,
        "c.yaml",
        f"""region: us-east-1
trackingId: {ns.tracking}
groupName: {main}
tags: {{Service: svc, Env: test}}
schedules:
  - name: {ns.p}-weekly
    description: weekly report
    scheduleExpression: cron(0 9 ? * MON *)
    scheduleExpressionTimezone: Asia/Tokyo
    state: ENABLED
    startDate: 2030-01-01T00:00:00Z
    endDate: "2031-06-30T12:00:00Z"
    flexibleTimeWindow: {{mode: FLEXIBLE, maximumWindowInMinutes: 15}}
    target:
      arn: {ns.sqs()}
      roleArn: {ns.role()}
      input: '{{"kind": "weekly", "n": 2}}'
  - name: {ns.p}-paused
    groupName: {side}
    scheduleExpression: rate(5 minutes)
    state: DISABLED
    flexibleTimeWindow: {{mode: OFF}}
    target: {{arn: "{ns.sqs()}", roleArn: "{ns.role()}", input: {{a: [1, true]}}}}
  - name: {ns.p}-existing
    groupName: {pre}
    scheduleExpression: rate(1 day)
    state: ENABLED
    flexibleTimeWindow: {{mode: "OFF"}}
    target: {{arn: "{ns.sqs()}", roleArn: "{ns.role()}"}}
  - name: {ns.p}-dflt
    groupName: default
    scheduleExpression: at(2035-01-01T00:00:00)
    state: ENABLED
    flexibleTimeWindow: {{mode: "OFF"}}
    target: {{arn: "{ns.sqs()}", roleArn: "{ns.role()}"}}
""",
    )
    result = apply(ws, "c.yaml")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        f"+ schedule:{ns.p}-dflt (create)",
        f"+ schedule-group:{main} (create)",
        f"+ schedule:{ns.p}-weekly (create)",
        f"+ schedule:{ns.p}-existing (create)",
        f"+ schedule-group:{side} (create)",
        f"+ schedule:{ns.p}-paused (create)",
    ]
    assert group_tags(main) == {"Service": "svc", "Env": "test", TRACKING_KEY: ns.tracking}
    assert group_tags(side) == {"Service": "svc", "Env": "test", TRACKING_KEY: ns.tracking}
    assert group_tags(pre) == {"Old": "1"}
    assert group_tags("default") == {}

    same(
        schedule_snapshot(main, f"{ns.p}-weekly"),
        {
            "Name": f"{ns.p}-weekly",
            "GroupName": main,
            "Description": "weekly report",
            "ScheduleExpression": "cron(0 9 ? * MON *)",
            "ScheduleExpressionTimezone": "Asia/Tokyo",
            "State": "ENABLED",
            "StartDate": "2030-01-01T00:00:00Z",
            "EndDate": "2031-06-30T12:00:00Z",
            "FlexibleTimeWindow": {"Mode": "FLEXIBLE", "MaximumWindowInMinutes": 15},
            "Target": {"Arn": ns.sqs(), "RoleArn": ns.role(), "Input": {"kind": "weekly", "n": 2}},
        },
    )
    same(
        schedule_snapshot(side, f"{ns.p}-paused"),
        {
            "Name": f"{ns.p}-paused",
            "GroupName": side,
            "ScheduleExpression": "rate(5 minutes)",
            "State": "DISABLED",
            "FlexibleTimeWindow": {"Mode": "OFF"},
            "Target": {"Arn": ns.sqs(), "RoleArn": ns.role(), "Input": {"a": [1, True]}},
        },
    )
    assert schedule_names(pre) == [f"{ns.p}-existing"]
    assert schedule_names("default", ns.p) == [f"{ns.p}-dflt"]

    clean = cli("-conf", "c.yaml", "diff", cwd=ws)
    assert (clean.returncode, clean.stdout) == (0, "")
    again = apply(ws, "c.yaml")
    assert again.stdout.splitlines() == [
        f"= schedule:{ns.p}-dflt (no-op)",
        f"= schedule:{ns.p}-weekly (no-op)",
        f"= schedule:{ns.p}-existing (no-op)",
        f"= schedule:{ns.p}-paused (no-op)",
    ]


def test_group_without_tags_or_tracking(ws, ns):
    group = f"{ns.p}-bare"
    write(
        ws,
        "c.yaml",
        f"""region: us-east-1
groupName: {group}
schedules:
  - name: {ns.p}-s
    scheduleExpression: rate(1 hour)
    state: ENABLED
    flexibleTimeWindow: {{mode: "OFF"}}
    target: {{arn: "{ns.sqs()}", roleArn: "{ns.role()}"}}
""",
    )
    result = apply(ws, "c.yaml")
    assert (result.returncode, result.stdout) == (0, f"+ schedule-group:{group} (create)\n+ schedule:{ns.p}-s (create)\n"), result.stderr
    assert group_tags(group) == {}


def test_service_defaults_are_canonicalized(ws, ns):
    group = f"{ns.p}-g"
    write(
        ws,
        "c.yaml",
        f"""region: us-east-1
groupName: {group}
schedules:
  - name: {ns.p}-once
    scheduleExpression: at(2035-06-01T12:00:00)
    scheduleExpressionTimezone: UTC
    actionAfterCompletion: NONE
    state: ENABLED
    startDate: "2030-01-01T00:00:00Z"
    flexibleTimeWindow: {{mode: "OFF"}}
    target:
      arn: {ns.sqs()}
      roleArn: {ns.role()}
      retryPolicy: {{maximumRetryAttempts: 185, maximumEventAgeInSeconds: 86400}}
  - name: {ns.p}-delete
    scheduleExpression: at(2035-06-01T12:00:00)
    actionAfterCompletion: DELETE
    state: ENABLED
    flexibleTimeWindow: {{mode: "OFF"}}
    target:
      arn: {ns.sqs()}
      roleArn: {ns.role()}
      retryPolicy: {{maximumRetryAttempts: 0, maximumEventAgeInSeconds: 60}}
""",
    )
    before = cli("-conf", "c.yaml", "diff", cwd=ws)
    assert before.returncode == 2
    assert not [line for line in before.stdout.splitlines() if "scheduleExpressionTimezone" in line or "NONE" in line], before.stdout
    assert_diff(
        before.stdout,
        f"""--- schedule:{ns.p}-once (current)
+++ schedule:{ns.p}-once (desired)
@@ -1 +1,11 @@
+name: {ns.p}-once
+groupName: {group}
+scheduleExpression: at(2035-06-01T12:00:00)
+state: ENABLED
+startDate: "2030-01-01T00:00:00Z"
+flexibleTimeWindow:
+  mode: "OFF"
+target:
+  arn: {ns.sqs()}
+  roleArn: {ns.role()}
{SP}
--- schedule:{ns.p}-delete (current)
+++ schedule:{ns.p}-delete (desired)
@@ -1 +1,14 @@
+name: {ns.p}-delete
+groupName: {group}
+scheduleExpression: at(2035-06-01T12:00:00)
+state: ENABLED
+actionAfterCompletion: DELETE
+flexibleTimeWindow:
+  mode: "OFF"
+target:
+  arn: {ns.sqs()}
+  roleArn: {ns.role()}
+  retryPolicy:
+    maximumRetryAttempts: 0
+    maximumEventAgeInSeconds: 60
{SP}
""",
    )

    assert apply(ws, "c.yaml").returncode == 0
    # Explicit defaults may or may not be sent: the snapshot treats Scheduler defaults as unset.
    common = {"GroupName": group, "ScheduleExpression": "at(2035-06-01T12:00:00)", "State": "ENABLED", "FlexibleTimeWindow": {"Mode": "OFF"}}
    same(
        schedule_snapshot(group, f"{ns.p}-once"),
        dict(common, Name=f"{ns.p}-once", StartDate="2030-01-01T00:00:00Z", Target={"Arn": ns.sqs(), "RoleArn": ns.role()}),
    )
    same(
        schedule_snapshot(group, f"{ns.p}-delete"),
        dict(
            common,
            Name=f"{ns.p}-delete",
            ActionAfterCompletion="DELETE",
            Target={"Arn": ns.sqs(), "RoleArn": ns.role(), "RetryPolicy": {"MaximumRetryAttempts": 0, "MaximumEventAgeInSeconds": 60}},
        ),
    )

    clean = cli("-conf", "c.yaml", "diff", cwd=ws)
    assert (clean.returncode, clean.stdout) == (0, "")
    assert apply(ws, "c.yaml").stdout == f"= schedule:{ns.p}-once (no-op)\n= schedule:{ns.p}-delete (no-op)\n"


def test_omitted_state_window_and_offset_dates_drift(ws, ns):
    group = f"{ns.p}-g"
    write(
        ws,
        "c.yaml",
        f"""region: us-east-1
groupName: {group}
schedules:
  - name: {ns.p}-a
    scheduleExpression: rate(7 days)
    target: {{arn: "{ns.sqs()}", roleArn: "{ns.role()}"}}
  - name: {ns.p}-b
    scheduleExpression: rate(1 hour)
    state: DISABLED
    startDate: 2030-01-01T09:00:00+09:00
    flexibleTimeWindow: {{mode: "OFF"}}
    target: {{arn: "{ns.sqs()}", roleArn: "{ns.role()}"}}
""",
    )
    assert apply(ws, "c.yaml").returncode == 0
    a = schedule_snapshot(group, f"{ns.p}-a")
    assert (a["State"], a["FlexibleTimeWindow"]) == ("ENABLED", {"Mode": "OFF"})
    assert schedule_snapshot(group, f"{ns.p}-b")["StartDate"] == "2030-01-01T00:00:00Z"

    diff = cli("-conf", "c.yaml", "diff", cwd=ws)
    assert diff.returncode == 2
    assert_diff(
        changed_lines(diff.stdout),
        """-state: ENABLED
-flexibleTimeWindow:
-  mode: "OFF"
-startDate: "2030-01-01T00:00:00Z"
+startDate: "2030-01-01T09:00:00+09:00"
""",
    )
    assert apply(ws, "c.yaml").stdout == f"~ schedule:{ns.p}-a (update)\n~ schedule:{ns.p}-b (update)\n"


def test_schedule_target_options(ws, ns):
    group = f"{ns.p}-g"
    bus = f"arn:aws:events:us-east-1:{ns.acct}:event-bus/default"
    write(
        ws,
        "c.yaml",
        f"""region: us-east-1
groupName: {group}
schedules:
  - name: {ns.p}-bus
    scheduleExpression: rate(7 days)
    state: ENABLED
    flexibleTimeWindow: {{mode: "OFF"}}
    target:
      arn: {bus}
      roleArn: {ns.role()}
      eventBridgeParameters: {{detailType: Tick, source: my.app}}
      input: {{x: 1}}
  - name: {ns.p}-fifo
    scheduleExpression: cron(15 3 * * ? *)
    scheduleExpressionTimezone: Europe/Paris
    state: ENABLED
    kmsKeyArn: arn:aws:kms:us-east-1:{ns.acct}:key/{ns.p}
    flexibleTimeWindow: {{mode: "OFF"}}
    target:
      arn: {ns.sqs('f.fifo')}
      roleArn: {ns.role()}
      sqsParameters: {{messageGroupId: g}}
      deadLetterConfig: {{arn: "{ns.sqs('dlq')}"}}
      retryPolicy: {{maximumRetryAttempts: 3, maximumEventAgeInSeconds: 3600}}
""",
    )
    assert apply(ws, "c.yaml").returncode == 0
    same(
        schedule_snapshot(group, f"{ns.p}-bus")["Target"],
        {"Arn": bus, "RoleArn": ns.role(), "Input": {"x": 1}, "EventBridgeParameters": {"DetailType": "Tick", "Source": "my.app"}},
    )
    fifo = schedule_snapshot(group, f"{ns.p}-fifo")
    assert fifo["KmsKeyArn"] == f"arn:aws:kms:us-east-1:{ns.acct}:key/{ns.p}"
    same(
        fifo["Target"],
        {
            "Arn": ns.sqs("f.fifo"),
            "RoleArn": ns.role(),
            "SqsParameters": {"MessageGroupId": "g"},
            "DeadLetterConfig": {"Arn": ns.sqs("dlq")},
            "RetryPolicy": {"MaximumRetryAttempts": 3, "MaximumEventAgeInSeconds": 3600},
        },
    )
    clean = cli("-conf", "c.yaml", "diff", cwd=ws)
    assert (clean.returncode, clean.stdout) == (0, "")


def test_out_of_band_schedule_change_is_reverted(ws, ns):
    group = f"{ns.p}-g"
    write(
        ws,
        "c.yaml",
        f"""region: us-east-1
groupName: {group}
schedules:
  - name: {ns.p}-a
    scheduleExpression: cron(0 9 ? * MON-FRI *)
    scheduleExpressionTimezone: Europe/Paris
    state: ENABLED
    flexibleTimeWindow: {{mode: FLEXIBLE, maximumWindowInMinutes: 10}}
    target: {{arn: "{ns.sqs()}", roleArn: "{ns.role()}", input: {{a: x}}}}
""",
    )
    assert apply(ws, "c.yaml").returncode == 0
    scheduler().update_schedule(
        Name=f"{ns.p}-a",
        GroupName=group,
        ScheduleExpression="rate(9 minutes)",
        State="DISABLED",
        Description="oob",
        FlexibleTimeWindow={"Mode": "OFF"},
        Target={"Arn": ns.sqs("other"), "RoleArn": ns.role(), "Input": '{"a": "y"}'},
    )
    result = apply(ws, "c.yaml")
    assert (result.returncode, result.stdout) == (0, f"~ schedule:{ns.p}-a (update)\n"), result.stderr
    same(
        schedule_snapshot(group, f"{ns.p}-a"),
        {
            "Name": f"{ns.p}-a",
            "GroupName": group,
            "ScheduleExpression": "cron(0 9 ? * MON-FRI *)",
            "ScheduleExpressionTimezone": "Europe/Paris",
            "State": "ENABLED",
            "FlexibleTimeWindow": {"Mode": "FLEXIBLE", "MaximumWindowInMinutes": 10},
            "Target": {"Arn": ns.sqs(), "RoleArn": ns.role(), "Input": {"a": "x"}},
        },
    )


def test_dry_run_creates_no_group_or_schedule(ws, ns):
    group, existing = f"{ns.p}-new", f"{ns.p}-old"
    create_group(existing)
    put_schedule(existing, f"{ns.p}-kept", ns.sqs(), ns.role(), expression="rate(3 hours)")
    write(
        ws,
        "c.yaml",
        f"""region: us-east-1
trackingId: {ns.tracking}
groupName: {group}
schedules:
  - name: {ns.p}-s
    scheduleExpression: rate(1 hour)
    state: ENABLED
    flexibleTimeWindow: {{mode: "OFF"}}
    target: {{arn: "{ns.sqs()}", roleArn: "{ns.role()}"}}
  - name: {ns.p}-kept
    groupName: {existing}
    scheduleExpression: rate(1 hour)
    state: ENABLED
    flexibleTimeWindow: {{mode: "OFF"}}
    target: {{arn: "{ns.sqs()}", roleArn: "{ns.role()}"}}
""",
    )
    before = schedule_snapshot(existing, f"{ns.p}-kept")
    result = cli("-conf", "c.yaml", "-dry-run", "apply", cwd=ws)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        f"+ schedule-group:{group} (create)",
        f"+ schedule:{ns.p}-s (create)",
        f"~ schedule:{ns.p}-kept (update)",
    ]
    assert group_tags(group) is None
    assert schedule_snapshot(existing, f"{ns.p}-kept") == before


def test_multi_group_example(ws, ns):
    # Adapted from the upstream examples/04-multi-group-schedules.yaml (region and targets rewritten).
    shared, team_a, team_b = f"{ns.p}-shared", f"{ns.p}-team-a", f"{ns.p}-team-b"
    write(
        ws,
        "04.yaml",
        f"""region: us-east-1
trackingId: {ns.tracking}
groupName: {shared}            # default for schedules without an override
tags:
  Owner: examples

schedules:
  - name: {ns.p}-heartbeat
    scheduleExpression: rate(5 minutes)
    state: ENABLED
    flexibleTimeWindow:
      mode: OFF
    target:
      arn: arn:aws:sqs:us-east-1:{{{{ must_env "AWS_ACCOUNT_ID" }}}}:{ns.p}-heartbeat
      roleArn: arn:aws:iam::{{{{ must_env "AWS_ACCOUNT_ID" }}}}:role/SchedulerInvoke

  - name: {ns.p}-team-a-batch
    groupName: {team_a}
    scheduleExpression: cron(0 2 * * ? *)
    scheduleExpressionTimezone: Asia/Tokyo
    state: ENABLED
    flexibleTimeWindow:
      mode: FLEXIBLE
      maximumWindowInMinutes: 15
    target:
      arn: arn:aws:sqs:us-east-1:{{{{ must_env "AWS_ACCOUNT_ID" }}}}:{ns.p}-team-a-batch
      roleArn: arn:aws:iam::{{{{ must_env "AWS_ACCOUNT_ID" }}}}:role/SchedulerInvoke

  - name: {ns.p}-team-b-batch
    groupName: {team_b}
    scheduleExpression: cron(0 4 * * ? *)
    scheduleExpressionTimezone: Asia/Tokyo
    state: ENABLED
    flexibleTimeWindow:
      mode: OFF
    target:
      arn: arn:aws:sqs:us-east-1:{{{{ must_env "AWS_ACCOUNT_ID" }}}}:{ns.p}-team-b-batch
      roleArn: arn:aws:iam::{{{{ must_env "AWS_ACCOUNT_ID" }}}}:role/SchedulerInvoke
""",
    )
    checked = cli("-conf", "04.yaml", "validate", cwd=ws)
    assert (checked.returncode, checked.stdout) == (0, "OK: 1 config file(s) valid\n"), checked.stderr
    result = apply(ws, "04.yaml")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        f"+ schedule-group:{shared} (create)",
        f"+ schedule:{ns.p}-heartbeat (create)",
        f"+ schedule-group:{team_a} (create)",
        f"+ schedule:{ns.p}-team-a-batch (create)",
        f"+ schedule-group:{team_b} (create)",
        f"+ schedule:{ns.p}-team-b-batch (create)",
    ]
    for group in (shared, team_a, team_b):
        assert group_tags(group) == {"Owner": "examples", TRACKING_KEY: ns.tracking}
    role = f"arn:aws:iam::{ns.acct}:role/SchedulerInvoke"
    same(
        schedule_snapshot(shared, f"{ns.p}-heartbeat"),
        {
            "Name": f"{ns.p}-heartbeat",
            "GroupName": shared,
            "ScheduleExpression": "rate(5 minutes)",
            "State": "ENABLED",
            "FlexibleTimeWindow": {"Mode": "OFF"},
            "Target": {"Arn": ns.sqs("heartbeat"), "RoleArn": role},
        },
    )
    same(
        schedule_snapshot(team_b, f"{ns.p}-team-b-batch"),
        {
            "Name": f"{ns.p}-team-b-batch",
            "GroupName": team_b,
            "ScheduleExpression": "cron(0 4 * * ? *)",
            "ScheduleExpressionTimezone": "Asia/Tokyo",
            "State": "ENABLED",
            "FlexibleTimeWindow": {"Mode": "OFF"},
            "Target": {"Arn": ns.sqs("team-b-batch"), "RoleArn": role},
        },
    )
    assert schedule_snapshot(team_a, f"{ns.p}-team-a-batch")["FlexibleTimeWindow"] == {"Mode": "FLEXIBLE", "MaximumWindowInMinutes": 15}
    clean = cli("-conf", "04.yaml", "diff", cwd=ws)
    assert (clean.returncode, clean.stdout) == (0, "")
