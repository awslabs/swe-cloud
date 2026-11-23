"""dump: current AWS state rendered as one configuration document."""

from support import TRACKING_KEY, cli, create_group, events, load_yaml, put_rule, put_schedule, same, scheduler, write


def seed(ws, ns, group):
    """Apply a known configuration and add out-of-band neighbours."""
    write(
        ws,
        "seed.yaml",
        f"""region: us-east-1
trackingId: {ns.tracking}
groupName: {group}
tags: {{Service: svc}}
rules:
  - name: {ns.p}-b-pattern
    eventPattern: '{{"source": ["x.app"], "detail": {{"n": [{{"numeric": [">", 5]}}]}}}}'
    state: DISABLED
    tags: {{Team: a}}
    targets:
      - {{id: q, arn: "{ns.sqs()}", input: '{{"b": 2.5, "a": true, "n": null, "big": 1e3}}'}}
  - name: {ns.p}-a-tick
    description: tick
    scheduleExpression: rate(5 minutes)
    state: ENABLED
    tags: {{Team: b}}
    targets:
      - {{id: z, arn: "{ns.sns()}", inputPath: $.detail}}
      - {{id: m, arn: "{ns.sqs()}", retryPolicy: {{maximumRetryAttempts: 2, maximumEventAgeInSeconds: 3600}}}}
schedules:
  - name: {ns.p}-weekly
    scheduleExpression: cron(0 9 ? * MON *)
    scheduleExpressionTimezone: Asia/Tokyo
    state: ENABLED
    startDate: 2030-01-01T09:00:00+09:00
    flexibleTimeWindow: {{mode: FLEXIBLE, maximumWindowInMinutes: 15}}
    target: {{arn: "{ns.sqs()}", roleArn: "{ns.role()}", input: {{kind: weekly}}}}
  - name: {ns.p}-default
    groupName: default
    scheduleExpression: rate(1 hour)
    state: ENABLED
    flexibleTimeWindow: {{mode: "OFF"}}
    target: {{arn: "{ns.sqs()}", roleArn: "{ns.role()}"}}
""",
    )
    result = cli("-conf", "seed.yaml", "-auto-approve", "apply", cwd=ws)
    assert result.returncode == 0, result.stderr
    put_rule(f"{ns.p}-c-foreign", state="DISABLED", tags={"Team": "a", "Owner": "other"})


def tick(ns):
    return {
        "name": f"{ns.p}-a-tick",
        "description": "tick",
        "scheduleExpression": "rate(5 minutes)",
        "state": "ENABLED",
        "tags": {"Service": "svc", "Team": "b"},
        "targets": [
            {"id": "m", "arn": ns.sqs(), "retryPolicy": {"maximumRetryAttempts": 2, "maximumEventAgeInSeconds": 3600}},
            {"id": "z", "arn": ns.sns(), "inputPath": "$.detail"},
        ],
    }


def pattern(ns):
    return {
        "name": f"{ns.p}-b-pattern",
        "eventPattern": {"detail": {"n": [{"numeric": [">", 5]}]}, "source": ["x.app"]},
        "state": "DISABLED",
        "tags": {"Service": "svc", "Team": "a"},
        "targets": [{"id": "q", "arn": ns.sqs(), "input": {"a": True, "b": 2.5, "big": 1000, "n": None}}],
    }


def foreign(ns):
    return {
        "name": f"{ns.p}-c-foreign",
        "scheduleExpression": "rate(1 hour)",
        "state": "DISABLED",
        "tags": {"Owner": "other", "Team": "a"},
        "targets": [],
    }


def weekly(ns, group):
    return {
        "name": f"{ns.p}-weekly",
        "groupName": group,
        "scheduleExpression": "cron(0 9 ? * MON *)",
        "scheduleExpressionTimezone": "Asia/Tokyo",
        "state": "ENABLED",
        "startDate": "2030-01-01T00:00:00Z",
        "flexibleTimeWindow": {"mode": "FLEXIBLE", "maximumWindowInMinutes": 15},
        "target": {"arn": ns.sqs(), "roleArn": ns.role(), "input": {"kind": "weekly"}},
    }


def dump(ws, *args):
    result = cli(*args, cwd=ws)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    return load_yaml(result.stdout)


def test_dump_reflects_rules_and_group_schedules(ws, ns):
    group = f"{ns.p}-g"
    seed(ws, ns, group)
    same(
        dump(ws, "-conf", "seed.yaml", "dump", ns.p),
        {
            "region": "us-east-1",
            "groupName": group,
            "rules": [tick(ns), pattern(ns), foreign(ns)],
            "schedules": [weekly(ns, group)],
        },
    )
    # The prefix narrows both sections.
    same(
        dump(ws, "-conf", "seed.yaml", "dump", f"{ns.p}-b"),
        {"region": "us-east-1", "groupName": group, "rules": [pattern(ns)]},
    )


def test_dump_tag_filters_apply_to_rules_only(ws, ns):
    group = f"{ns.p}-g"
    seed(ws, ns, group)
    same(
        dump(ws, "-conf", "seed.yaml", "-tag", "Team=a", "dump", ns.p),
        {"region": "us-east-1", "groupName": group, "rules": [pattern(ns), foreign(ns)], "schedules": [weekly(ns, group)]},
    )
    same(
        dump(ws, "-conf", "seed.yaml", "-tag", "Team=a", "-tag", "Owner=other", "dump", ns.p),
        {"region": "us-east-1", "groupName": group, "rules": [foreign(ns)], "schedules": [weekly(ns, group)]},
    )
    same(
        dump(ws, "-conf", "seed.yaml", "-tag", f"{TRACKING_KEY}={ns.tracking}", "dump", ns.p),
        {"region": "us-east-1", "groupName": group, "rules": [tick(ns), pattern(ns)], "schedules": [weekly(ns, group)]},
    )
    same(
        dump(ws, "-conf", "seed.yaml", "-tag", "Team=a", "-tag", "Team=zzz", "dump", ns.p),
        {"region": "us-east-1", "groupName": group, "schedules": [weekly(ns, group)]},
    )


def test_dump_uses_bus_and_group_of_first_config(ws, ns):
    bus, group = f"{ns.p}-bus", f"{ns.p}-g"
    events().create_event_bus(Name=bus)
    put_rule(f"{ns.p}-onbus", bus=bus, expression="rate(2 hours)", targets=[{"Id": "q", "Arn": ns.sqs()}])
    put_rule(f"{ns.p}-ondefault", targets=[{"Id": "q", "Arn": ns.sqs()}])
    create_group(group)
    put_schedule(group, f"{ns.p}-ingroup", ns.sqs(), ns.role())
    put_schedule("default", f"{ns.p}-indefault", ns.sqs(), ns.role())
    write(ws, "conf/a.yaml", f"region: us-east-1\neventBusName: {bus}\ngroupName: {group}\nrules: []\n")
    write(ws, "conf/b.yaml", "region: us-east-1\nschedules: []\n")
    onbus = {
        "name": f"{ns.p}-onbus",
        "scheduleExpression": "rate(2 hours)",
        "state": "ENABLED",
        "targets": [{"id": "q", "arn": ns.sqs()}],
    }
    ingroup = {
        "name": f"{ns.p}-ingroup",
        "groupName": group,
        "scheduleExpression": "rate(1 hour)",
        "state": "ENABLED",
        "flexibleTimeWindow": {"mode": "OFF"},
        "target": {"arn": ns.sqs(), "roleArn": ns.role()},
    }
    same(
        dump(ws, "-conf", "conf/*.yaml", "dump", ns.p),
        {"region": "us-east-1", "eventBusName": bus, "groupName": group, "rules": [onbus], "schedules": [ingroup]},
    )
    # Without a configuration file the defaults apply: default bus, default group, region from the environment.
    ondefault = dict(onbus, name=f"{ns.p}-ondefault", scheduleExpression="rate(1 hour)")
    indefault = dict(ingroup, name=f"{ns.p}-indefault", groupName="default")
    same(dump(ws, "dump", ns.p), {"rules": [ondefault], "schedules": [indefault]})
    same(dump(ws, "-conf", "missing.yaml", "dump", f"{ns.p}-nothing"), {})


def test_dump_output_reapplies_cleanly(ws, ns):
    group = f"{ns.p}-g"
    seed(ws, ns, group)
    scheduler().delete_schedule(GroupName="default", Name=f"{ns.p}-default")
    events().delete_rule(Name=f"{ns.p}-c-foreign")
    result = cli("-conf", "seed.yaml", "dump", ns.p, cwd=ws)
    assert result.returncode == 0, result.stderr
    write(ws, "dumped.yaml", result.stdout)
    diff = cli("-conf", "dumped.yaml", "diff", cwd=ws)
    assert (diff.returncode, diff.stdout) == (0, ""), diff.stdout


def test_dump_renders_every_target_and_schedule_field(ws, ns):
    group = f"{ns.p}-g"
    sm = f"arn:aws:states:us-east-1:{ns.acct}:stateMachine:{ns.p}-sm"
    bus = f"arn:aws:events:us-east-1:{ns.acct}:event-bus/default"
    kms = f"arn:aws:kms:us-east-1:{ns.acct}:key/{ns.p}"
    write(
        ws,
        "c.yaml",
        f"""region: us-east-1
groupName: {group}
rules:
  - name: {ns.p}-r
    eventPattern: {{source: [my.app]}}
    state: ENABLED
    roleArn: {ns.role('events')}
    targets:
      - id: fifo
        arn: {ns.sqs('f.fifo')}
        sqsParameters: {{messageGroupId: grp1}}
        deadLetterConfig: {{arn: "{ns.sqs('dlq')}"}}
      - id: sfn
        arn: {sm}
        roleArn: {ns.role('sfn')}
        inputTransformer:
          inputPathsMap: {{b: $.detail.bucket}}
          inputTemplate: '{{"bucket": <b>}}'
schedules:
  - name: {ns.p}-s
    description: every week
    scheduleExpression: rate(7 days)
    state: DISABLED
    startDate: "2030-01-01T00:00:00Z"
    endDate: 2031-06-30T12:00:00Z
    kmsKeyArn: {kms}
    flexibleTimeWindow: {{mode: "OFF"}}
    target:
      arn: {bus}
      roleArn: {ns.role()}
      input: {{x: [1, "two"]}}
      deadLetterConfig: {{arn: "{ns.sqs('dlq')}"}}
      retryPolicy: {{maximumRetryAttempts: 3, maximumEventAgeInSeconds: 3600}}
      eventBridgeParameters: {{detailType: Tick, source: my.app}}
  - name: {ns.p}-q
    scheduleExpression: cron(0 3 * * ? *)
    scheduleExpressionTimezone: Europe/Paris
    state: ENABLED
    flexibleTimeWindow: {{mode: FLEXIBLE, maximumWindowInMinutes: 5}}
    target:
      arn: {ns.sqs('f.fifo')}
      roleArn: {ns.role()}
      sqsParameters: {{messageGroupId: g}}
""",
    )
    applied = cli("-conf", "c.yaml", "-auto-approve", "apply", cwd=ws)
    assert applied.returncode == 0, applied.stderr
    same(
        dump(ws, "-conf", "c.yaml", "dump", ns.p),
        {
            "region": "us-east-1",
            "groupName": group,
            "rules": [
                {
                    "name": f"{ns.p}-r",
                    "eventPattern": {"source": ["my.app"]},
                    "state": "ENABLED",
                    "roleArn": ns.role("events"),
                    "targets": [
                        {
                            "id": "fifo",
                            "arn": ns.sqs("f.fifo"),
                            "deadLetterConfig": {"arn": ns.sqs("dlq")},
                            "sqsParameters": {"messageGroupId": "grp1"},
                        },
                        {
                            "id": "sfn",
                            "arn": sm,
                            "roleArn": ns.role("sfn"),
                            "inputTransformer": {"inputPathsMap": {"b": "$.detail.bucket"}, "inputTemplate": '{"bucket": <b>}'},
                        },
                    ],
                }
            ],
            "schedules": [
                {
                    "name": f"{ns.p}-q",
                    "groupName": group,
                    "scheduleExpression": "cron(0 3 * * ? *)",
                    "scheduleExpressionTimezone": "Europe/Paris",
                    "state": "ENABLED",
                    "flexibleTimeWindow": {"mode": "FLEXIBLE", "maximumWindowInMinutes": 5},
                    "target": {"arn": ns.sqs("f.fifo"), "roleArn": ns.role(), "sqsParameters": {"messageGroupId": "g"}},
                },
                {
                    "name": f"{ns.p}-s",
                    "description": "every week",
                    "groupName": group,
                    "scheduleExpression": "rate(7 days)",
                    "state": "DISABLED",
                    "startDate": "2030-01-01T00:00:00Z",
                    "endDate": "2031-06-30T12:00:00Z",
                    "kmsKeyArn": kms,
                    "flexibleTimeWindow": {"mode": "OFF"},
                    "target": {
                        "arn": bus,
                        "roleArn": ns.role(),
                        "input": {"x": [1, "two"]},
                        "deadLetterConfig": {"arn": ns.sqs("dlq")},
                        "retryPolicy": {"maximumRetryAttempts": 3, "maximumEventAgeInSeconds": 3600},
                        "eventBridgeParameters": {"detailType": "Tick", "source": "my.app"},
                    },
                },
            ],
        },
    )
