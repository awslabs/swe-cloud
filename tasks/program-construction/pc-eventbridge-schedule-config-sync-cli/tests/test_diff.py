"""diff: rendered unified diffs between current AWS state and the desired configuration."""

from support import assert_diff, cli, events, scheduler, write

# The context line a unified diff shows for the empty final line of both sides.
SP = " "


def config(ns, group):
    return f"""region: us-east-1
trackingId: {ns.tracking}
groupName: {group}
tags: {{Service: svc}}
rules:
  - name: {ns.p}-r
    description: nightly job
    scheduleExpression: cron(0 18 * * ? *)
    state: ENABLED
    tags: {{Team: a}}
    targets:
      - id: b
        arn: {ns.sqs('b')}
        retryPolicy: {{maximumEventAgeInSeconds: 60, maximumRetryAttempts: 1}}
      - id: a
        arn: {ns.sqs('a')}
        input: '{{"list": ["x", true], "count": 2}}'
schedules:
  - name: {ns.p}-s
    description: daily
    scheduleExpression: cron(0 9 * * ? *)
    scheduleExpressionTimezone: Asia/Tokyo
    state: ENABLED
    flexibleTimeWindow: {{mode: "OFF"}}
    target:
      arn: {ns.sqs()}
      roleArn: {ns.role()}
      input: {{kind: daily}}
"""


def test_creation_diff(ws, ns):
    group = f"{ns.p}-g"
    write(ws, "c.yaml", config(ns, group))
    result = cli("-conf", "c.yaml", "diff", cwd=ws)
    assert result.returncode == 2, result.stderr
    assert result.stderr == ""
    assert_diff(
        result.stdout,
        f"""--- rule:{ns.p}-r (current)
+++ rule:{ns.p}-r (desired)
@@ -1 +1,21 @@
+name: {ns.p}-r
+description: nightly job
+scheduleExpression: cron(0 18 * * ? *)
+state: ENABLED
+tags:
+  Service: svc
+  Team: a
+targets:
+  - id: a
+    arn: {ns.sqs('a')}
+    input:
+      count: 2
+      list:
+        - x
+        - true
+  - id: b
+    arn: {ns.sqs('b')}
+    retryPolicy:
+      maximumRetryAttempts: 1
+      maximumEventAgeInSeconds: 60
{SP}
--- schedule:{ns.p}-s (current)
+++ schedule:{ns.p}-s (desired)
@@ -1 +1,14 @@
+name: {ns.p}-s
+description: daily
+groupName: {group}
+scheduleExpression: cron(0 9 * * ? *)
+scheduleExpressionTimezone: Asia/Tokyo
+state: ENABLED
+flexibleTimeWindow:
+  mode: "OFF"
+target:
+  arn: {ns.sqs()}
+  roleArn: {ns.role()}
+  input:
+    kind: daily
{SP}
""",
    )
    assert events().list_rules(NamePrefix=ns.p)["Rules"] == []


def test_drift_diff_after_out_of_band_changes(ws, ns):
    group = f"{ns.p}-g"
    write(ws, "c.yaml", config(ns, group))
    applied = cli("-conf", "c.yaml", "-auto-approve", "apply", cwd=ws)
    assert applied.returncode == 0, applied.stderr
    clean = cli("-conf", "c.yaml", "diff", cwd=ws)
    assert (clean.returncode, clean.stdout, clean.stderr) == (0, "", "")

    client = events()
    arn = client.describe_rule(Name=f"{ns.p}-r")["Arn"]
    client.tag_resource(ResourceARN=arn, Tags=[{"Key": "Stray", "Value": "1"}, {"Key": "Team", "Value": "b"}])
    client.put_rule(Name=f"{ns.p}-r", ScheduleExpression="cron(0 18 * * ? *)", State="DISABLED", Description="nightly job")
    scheduler().update_schedule(
        Name=f"{ns.p}-s",
        GroupName=group,
        ScheduleExpression="cron(0 9 * * ? *)",
        ScheduleExpressionTimezone="Asia/Tokyo",
        State="ENABLED",
        Description="daily",
        FlexibleTimeWindow={"Mode": "OFF"},
        Target={"Arn": ns.sqs(), "RoleArn": ns.role(), "Input": '{"kind": "weekly"}'},
    )
    result = cli("-conf", "c.yaml", "diff", cwd=ws)
    assert result.returncode == 2, result.stderr
    assert_diff(
        result.stdout,
        f"""--- rule:{ns.p}-r (current)
+++ rule:{ns.p}-r (desired)
@@ -1,11 +1,10 @@
 name: {ns.p}-r
 description: nightly job
 scheduleExpression: cron(0 18 * * ? *)
-state: DISABLED
+state: ENABLED
 tags:
   Service: svc
-  Stray: "1"
-  Team: b
+  Team: a
 targets:
   - id: a
     arn: {ns.sqs('a')}
--- schedule:{ns.p}-s (current)
+++ schedule:{ns.p}-s (desired)
@@ -10,5 +10,5 @@
   arn: {ns.sqs()}
   roleArn: {ns.role()}
   input:
-    kind: weekly
+    kind: daily
{SP}
""",
    )


def test_target_filter_limits_diff(ws, ns):
    group = f"{ns.p}-g"
    write(ws, "c.yaml", config(ns, group))
    only_schedule = cli("-conf", "c.yaml", "-target", f"schedule:{ns.p}-s", "diff", cwd=ws)
    assert only_schedule.returncode == 2
    assert [line for line in only_schedule.stdout.splitlines() if line.startswith(("--- ", "+++ "))] == [
        f"--- schedule:{ns.p}-s (current)",
        f"+++ schedule:{ns.p}-s (desired)",
    ]
    applied = cli("-conf", "c.yaml", "-target", f"rule:{ns.p}-r", "-auto-approve", "apply", cwd=ws)
    assert (applied.returncode, applied.stdout) == (0, f"+ rule:{ns.p}-r (create)\n"), applied.stderr
    in_sync = cli("-conf", "c.yaml", "-target", f"rule:{ns.p}-r", "diff", cwd=ws)
    assert (in_sync.returncode, in_sync.stdout) == (0, "")
    both = cli("-conf", "c.yaml", "-target", f"rule:{ns.p}-r", "-target", f"schedule:{ns.p}-s", "diff", cwd=ws)
    assert both.returncode == 2
    assert [line for line in both.stdout.splitlines() if line.startswith("+++ ")] == [f"+++ schedule:{ns.p}-s (desired)"]


def test_multi_file_sections(ws, ns):
    write(
        ws,
        "conf/01-rules.yaml",
        f"""region: us-east-1
rules:
  - name: {ns.p}-one
    eventPattern: {{source: [one]}}
    state: ENABLED
    targets: [{{id: q, arn: "{ns.sqs()}"}}]
""",
    )
    write(ws, "conf/02-empty.yaml", "\n")
    write(
        ws,
        "conf/03-none.yaml",
        f"""region: us-east-1
rules: []
schedules: []
""",
    )
    result = cli("-conf", "conf/*.yaml", "diff", cwd=ws)
    assert result.returncode == 2, result.stderr
    assert_diff(
        result.stdout,
        f"""# conf/01-rules.yaml
--- rule:{ns.p}-one (current)
+++ rule:{ns.p}-one (desired)
@@ -1 +1,9 @@
+name: {ns.p}-one
+eventPattern:
+  source:
+    - one
+state: ENABLED
+targets:
+  - id: q
+    arn: {ns.sqs()}
{SP}
# conf/03-none.yaml
""",
    )
    applied = cli("-conf", "conf/*.yaml", "-auto-approve", "apply", cwd=ws)
    assert applied.returncode == 0, applied.stderr
    assert applied.stdout == f"# conf/01-rules.yaml\n+ rule:{ns.p}-one (create)\n# conf/03-none.yaml\n"
    assert applied.stderr == "applied 2 config file(s)\n"
    clean = cli("-conf", "conf/*.yaml", "diff", cwd=ws)
    assert (clean.returncode, clean.stdout) == (0, "# conf/01-rules.yaml\n# conf/03-none.yaml\n")
    # A -target must name a resource of every loaded file; the failure comes after earlier files' output.
    targeted = cli("-conf", "conf/*.yaml", "-target", f"rule:{ns.p}-one", "diff", cwd=ws)
    assert targeted.returncode == 1
    assert targeted.stdout == "# conf/01-rules.yaml\n"
    assert targeted.stderr == f"error: -target rule:{ns.p}-one not found in config\n"
