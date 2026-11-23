"""EventBridge Rules: individual apply operations and the AWS state they leave."""

from support import TRACKING_KEY, assert_diff, changed_lines, cli, events, put_rule, rule_names, rule_snapshot, same, write


def apply(ws, conf, *flags):
    return cli("-conf", conf, *flags, "-auto-approve", "apply", cwd=ws)


def test_create_rules_with_merged_tags_and_sorted_targets(ws, ns):
    write(
        ws,
        "c.yaml",
        f"""region: us-east-1
trackingId: {ns.tracking}
tags: {{Service: svc, Team: base}}
rules:
  - name: {ns.p}-tick
    description: hourly tick
    scheduleExpression: rate(1 hour)
    state: ENABLED
    roleArn: {ns.role('events')}
    tags: {{Team: override, Extra: "1"}}
    targets:
      - id: zeta
        arn: {ns.sqs('z')}
        input: '{{"z": 1, "a": {{"y": [true, "s", null]}}, "f": -2}}'
      - id: alpha
        arn: {ns.sns()}
        input:
          msg: hello
          count: 3
          on: false
  - name: {ns.p}-plain
    scheduleExpression: cron(0 18 ? * MON-FRI *)
    state: DISABLED
    targets:
      - {{id: only, arn: "{ns.sqs()}"}}
""",
    )
    result = apply(ws, "c.yaml")
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"+ rule:{ns.p}-tick (create)\n+ rule:{ns.p}-plain (create)\n"
    assert result.stderr == ""

    tick = rule_snapshot(f"{ns.p}-tick")
    same(
        tick,
        {
            "rule": {
                "Name": f"{ns.p}-tick",
                "State": "ENABLED",
                "Description": "hourly tick",
                "ScheduleExpression": "rate(1 hour)",
                "RoleArn": ns.role("events"),
            },
            "tags": {"Service": "svc", "Team": "override", "Extra": "1", TRACKING_KEY: ns.tracking},
            "targets": {
                "zeta": {"Arn": ns.sqs("z"), "Input": {"z": 1, "a": {"y": [True, "s", None]}, "f": -2}},
                "alpha": {"Arn": ns.sns(), "Input": {"msg": "hello", "count": 3, "on": False}},
            },
        },
    )
    plain = rule_snapshot(f"{ns.p}-plain")
    same(
        plain,
        {
            "rule": {"Name": f"{ns.p}-plain", "State": "DISABLED", "ScheduleExpression": "cron(0 18 ? * MON-FRI *)"},
            "tags": {"Service": "svc", "Team": "base", TRACKING_KEY: ns.tracking},
            "targets": {"only": {"Arn": ns.sqs()}},
        },
    )
    again = apply(ws, "c.yaml")
    assert (again.returncode, again.stdout) == (0, f"= rule:{ns.p}-tick (no-op)\n= rule:{ns.p}-plain (no-op)\n")
    assert rule_snapshot(f"{ns.p}-tick") == tick


def test_event_pattern_forms_are_equivalent(ws, ns):
    pattern = {"source": ["billing.app"], "detail-type": ["Invoice Created"], "detail": {"n": [{"numeric": [">", 5]}]}}
    write(
        ws,
        "mapping.yaml",
        f"""region: us-east-1
rules:
  - name: {ns.p}-onevent
    eventPattern:
      source: [billing.app]
      detail-type: [Invoice Created]
      detail: {{n: [{{numeric: [">", 5]}}]}}
    state: ENABLED
    targets:
      - {{id: t, arn: "{ns.sns()}"}}
""",
    )
    write(
        ws,
        "string.yaml",
        f"""region: us-east-1
rules:
  - name: {ns.p}-onevent
    eventPattern: '{{ "detail": {{"n": [{{"numeric": [">", 5]}}]}},
      "detail-type": ["Invoice Created"], "source": ["billing.app"] }}'
    state: ENABLED
    targets:
      - {{id: t, arn: "{ns.sns()}"}}
""",
    )
    created = apply(ws, "mapping.yaml")
    assert (created.returncode, created.stdout) == (0, f"+ rule:{ns.p}-onevent (create)\n"), created.stderr
    snapshot = rule_snapshot(f"{ns.p}-onevent")
    same(snapshot["rule"], {"Name": f"{ns.p}-onevent", "State": "ENABLED", "EventPattern": pattern})
    same(snapshot["tags"], {})
    same(snapshot["targets"], {"t": {"Arn": ns.sns()}})

    clean = cli("-conf", "string.yaml", "diff", cwd=ws)
    assert (clean.returncode, clean.stdout) == (0, "")
    again = apply(ws, "string.yaml")
    assert (again.returncode, again.stdout) == (0, f"= rule:{ns.p}-onevent (no-op)\n")


def test_target_options_round_trip(ws, ns):
    acct = ns.acct
    write(
        ws,
        "c.yaml",
        f"""region: us-east-1
rules:
  - name: {ns.p}-opts
    eventPattern: {{source: [my.app]}}
    state: ENABLED
    targets:
      - id: fifo
        arn: {ns.sqs('f.fifo')}
        sqsParameters: {{messageGroupId: grp1}}
        input: {{kind: fifo}}
      - id: dlq
        arn: {ns.sns()}
        inputPath: $.detail
        retryPolicy: {{maximumRetryAttempts: 2, maximumEventAgeInSeconds: 3600}}
        deadLetterConfig: {{arn: "{ns.sqs('dlq')}"}}
      - id: sfn
        arn: arn:aws:states:us-east-1:{acct}:stateMachine:{ns.p}-sm
        roleArn: {ns.role('sfn')}
        inputTransformer:
          inputPathsMap: {{b: $.detail.bucket, id: $.id}}
          inputTemplate: '{{"bucket": <b>, "id": <id>}}'
      - id: stream
        arn: arn:aws:kinesis:us-east-1:{acct}:stream/{ns.p}-k
        roleArn: {ns.role('kin')}
""",
    )
    result = apply(ws, "c.yaml")
    assert (result.returncode, result.stdout) == (0, f"+ rule:{ns.p}-opts (create)\n"), result.stderr
    same(
        rule_snapshot(f"{ns.p}-opts")["targets"],
        {
            "fifo": {"Arn": ns.sqs("f.fifo"), "Input": {"kind": "fifo"}, "SqsParameters": {"MessageGroupId": "grp1"}},
            "dlq": {
                "Arn": ns.sns(),
                "InputPath": "$.detail",
                "RetryPolicy": {"MaximumRetryAttempts": 2, "MaximumEventAgeInSeconds": 3600},
                "DeadLetterConfig": {"Arn": ns.sqs("dlq")},
            },
            "sfn": {
                "Arn": f"arn:aws:states:us-east-1:{acct}:stateMachine:{ns.p}-sm",
                "RoleArn": ns.role("sfn"),
                "InputTransformer": {"InputPathsMap": {"b": "$.detail.bucket", "id": "$.id"}, "InputTemplate": '{"bucket": <b>, "id": <id>}'},
            },
            "stream": {"Arn": f"arn:aws:kinesis:us-east-1:{acct}:stream/{ns.p}-k", "RoleArn": ns.role("kin")},
        },
    )
    clean = cli("-conf", "c.yaml", "diff", cwd=ws)
    assert (clean.returncode, clean.stdout) == (0, "")
    assert apply(ws, "c.yaml").stdout == f"= rule:{ns.p}-opts (no-op)\n"


def test_update_replaces_targets_tags_and_description(ws, ns):
    write(
        ws,
        "v1.yaml",
        f"""region: us-east-1
trackingId: {ns.tracking}
tags: {{Service: svc}}
rules:
  - name: {ns.p}-r
    description: first
    scheduleExpression: rate(1 hour)
    state: ENABLED
    tags: {{Team: a}}
    targets:
      - {{id: a, arn: "{ns.sqs('a')}", input: {{v: 1}}}}
      - {{id: b, arn: "{ns.sqs('b')}"}}
""",
    )
    write(
        ws,
        "v2.yaml",
        f"""region: us-east-1
trackingId: {ns.tracking}
rules:
  - name: {ns.p}-r
    scheduleExpression: rate(2 hours)
    state: DISABLED
    targets:
      - {{id: b, arn: "{ns.sqs('b2')}", input: '{{"v": 2}}'}}
      - {{id: c, arn: "{ns.sns()}"}}
""",
    )
    assert apply(ws, "v1.yaml").returncode == 0
    dry = cli("-conf", "v2.yaml", "-dry-run", "apply", cwd=ws)
    assert (dry.returncode, dry.stdout) == (0, f"~ rule:{ns.p}-r (update)\n")
    assert rule_snapshot(f"{ns.p}-r")["rule"]["Description"] == "first"

    updated = apply(ws, "v2.yaml")
    assert (updated.returncode, updated.stdout) == (0, f"~ rule:{ns.p}-r (update)\n"), updated.stderr
    same(
        rule_snapshot(f"{ns.p}-r"),
        {
            "rule": {"Name": f"{ns.p}-r", "State": "DISABLED", "ScheduleExpression": "rate(2 hours)"},
            "tags": {TRACKING_KEY: ns.tracking},
            "targets": {"b": {"Arn": ns.sqs("b2"), "Input": {"v": 2}}, "c": {"Arn": ns.sns()}},
        },
    )
    assert apply(ws, "v2.yaml").stdout == f"= rule:{ns.p}-r (no-op)\n"


def test_out_of_band_changes_are_reverted(ws, ns):
    write(
        ws,
        "c.yaml",
        f"""region: us-east-1
trackingId: {ns.tracking}
rules:
  - name: {ns.p}-r
    description: keep
    scheduleExpression: rate(5 minutes)
    state: ENABLED
    tags: {{Team: a}}
    targets:
      - {{id: q, arn: "{ns.sqs()}"}}
""",
    )
    assert apply(ws, "c.yaml").returncode == 0
    client = events()
    arn = client.describe_rule(Name=f"{ns.p}-r")["Arn"]
    client.tag_resource(ResourceARN=arn, Tags=[{"Key": "Stray", "Value": "1"}, {"Key": "Team", "Value": "b"}])
    client.put_rule(Name=f"{ns.p}-r", ScheduleExpression="rate(5 minutes)", State="DISABLED", Description="keep")
    client.put_targets(Rule=f"{ns.p}-r", Targets=[{"Id": "extra", "Arn": ns.sns()}])

    result = apply(ws, "c.yaml")
    assert (result.returncode, result.stdout) == (0, f"~ rule:{ns.p}-r (update)\n"), result.stderr
    same(
        rule_snapshot(f"{ns.p}-r"),
        {
            "rule": {"Name": f"{ns.p}-r", "State": "ENABLED", "Description": "keep", "ScheduleExpression": "rate(5 minutes)"},
            "tags": {"Team": "a", TRACKING_KEY: ns.tracking},
            "targets": {"q": {"Arn": ns.sqs()}},
        },
    )


def test_dry_run_creates_nothing(ws, ns):
    write(
        ws,
        "c.yaml",
        f"""region: us-east-1
trackingId: {ns.tracking}
rules:
  - name: {ns.p}-a
    scheduleExpression: rate(1 hour)
    state: ENABLED
    targets: [{{id: q, arn: "{ns.sqs()}"}}]
  - name: {ns.p}-b
    eventPattern: {{source: [x]}}
    state: ENABLED
    targets: [{{id: q, arn: "{ns.sqs()}"}}]
""",
    )
    put_rule(f"{ns.p}-b", tags={"Old": "1"}, targets=[{"Id": "old", "Arn": ns.sns()}])
    before = rule_snapshot(f"{ns.p}-b")
    result = cli("-conf", "c.yaml", "-dry-run", "apply", cwd=ws)
    assert (result.returncode, result.stdout) == (0, f"+ rule:{ns.p}-a (create)\n~ rule:{ns.p}-b (update)\n"), result.stderr
    assert rule_names(ns.p) == [f"{ns.p}-b"]
    assert rule_snapshot(f"{ns.p}-b") == before


def test_omitted_state_is_never_in_sync(ws, ns):
    write(
        ws,
        "c.yaml",
        f"""region: us-east-1
rules:
  - name: {ns.p}-r
    scheduleExpression: rate(1 hour)
    targets: [{{id: q, arn: "{ns.sqs()}"}}]
""",
    )
    assert apply(ws, "c.yaml").stdout == f"+ rule:{ns.p}-r (create)\n"
    assert rule_snapshot(f"{ns.p}-r")["rule"]["State"] == "ENABLED"
    diff = cli("-conf", "c.yaml", "diff", cwd=ws)
    assert diff.returncode == 2
    assert_diff(changed_lines(diff.stdout), "-state: ENABLED\n")
    assert apply(ws, "c.yaml").stdout == f"~ rule:{ns.p}-r (update)\n"


def test_tags_without_tracking_id(ws, ns):
    put_rule(f"{ns.p}-r", tags={TRACKING_KEY: "someone", "Stray": "1"}, targets=[{"Id": "q", "Arn": ns.sqs()}])
    write(
        ws,
        "c.yaml",
        f"""region: us-east-1
tags: {{Service: svc}}
rules:
  - name: {ns.p}-r
    scheduleExpression: rate(1 hour)
    state: ENABLED
    targets: [{{id: q, arn: "{ns.sqs()}"}}]
  - name: {ns.p}-new
    scheduleExpression: rate(1 hour)
    state: ENABLED
    targets: [{{id: q, arn: "{ns.sqs()}"}}]
""",
    )
    result = apply(ws, "c.yaml")
    assert (result.returncode, result.stdout) == (0, f"~ rule:{ns.p}-r (update)\n+ rule:{ns.p}-new (create)\n"), result.stderr
    # Without a trackingId the existing tracking tag is left alone and none is added.
    assert rule_snapshot(f"{ns.p}-r")["tags"] == {TRACKING_KEY: "someone", "Service": "svc"}
    assert rule_snapshot(f"{ns.p}-new")["tags"] == {"Service": "svc"}



def test_updated_rules_take_the_tracking_id(ws, ns):
    # Upstream main_test.go reconcileTags: "trackingValue overrides stale tracking tag value".
    put_rule(f"{ns.p}-stale", tags={TRACKING_KEY: "stale", "Stray": "1"}, targets=[{"Id": "q", "Arn": ns.sqs()}])
    put_rule(f"{ns.p}-bare", targets=[{"Id": "q", "Arn": ns.sqs()}])
    write(
        ws,
        "c.yaml",
        f"""region: us-east-1
trackingId: {ns.tracking}
tags: {{Service: svc}}
rules:
  - name: {ns.p}-stale
    scheduleExpression: rate(2 hours)
    state: ENABLED
    targets: [{{id: q, arn: "{ns.sqs()}"}}]
  - name: {ns.p}-bare
    scheduleExpression: rate(2 hours)
    state: ENABLED
    targets: [{{id: q, arn: "{ns.sqs()}"}}]
""",
    )
    result = apply(ws, "c.yaml")
    assert (result.returncode, result.stdout) == (0, f"~ rule:{ns.p}-stale (update)\n~ rule:{ns.p}-bare (update)\n"), result.stderr
    for name in (f"{ns.p}-stale", f"{ns.p}-bare"):
        same(
            rule_snapshot(name),
            {
                "rule": {"Name": name, "State": "ENABLED", "ScheduleExpression": "rate(2 hours)"},
                "tags": {"Service": "svc", TRACKING_KEY: ns.tracking},
                "targets": {"q": {"Arn": ns.sqs()}},
            },
        )
    # Both are now owned by this trackingId, so an empty rules section prunes them.
    write(ws, "empty.yaml", f"region: us-east-1\ntrackingId: {ns.tracking}\nrules: []\n")
    pruned = apply(ws, "empty.yaml", "-prune")
    assert (pruned.returncode, pruned.stdout) == (0, f"- rule:{ns.p}-bare (delete)\n- rule:{ns.p}-stale (delete)\n"), pruned.stderr
    assert rule_names(ns.p) == []

def test_existing_identical_rule_is_not_retagged(ws, ns):
    put_rule(f"{ns.p}-r", expression="rate(2 hours)", targets=[{"Id": "q", "Arn": ns.sqs()}])
    write(
        ws,
        "c.yaml",
        f"""region: us-east-1
trackingId: {ns.tracking}
rules:
  - name: {ns.p}-r
    scheduleExpression: rate(2 hours)
    state: ENABLED
    tags: {{}}
    targets: [{{id: q, arn: "{ns.sqs()}"}}]
""",
    )
    result = apply(ws, "c.yaml")
    assert (result.returncode, result.stdout) == (0, f"= rule:{ns.p}-r (no-op)\n")
    # The tracking tag is not part of the compared state, so an in-sync rule stays untracked.
    assert rule_snapshot(f"{ns.p}-r")["tags"] == {}


def test_rules_on_a_custom_bus(ws, ns):
    bus = f"{ns.p}-bus"
    events().create_event_bus(Name=bus)
    put_rule(f"{ns.p}-r", targets=[{"Id": "q", "Arn": ns.sqs()}])
    write(
        ws,
        "c.yaml",
        f"""region: us-east-1
eventBusName: {bus}
rules:
  - name: {ns.p}-r
    eventPattern: {{source: ["y"]}}
    state: ENABLED
    targets: [{{id: q, arn: "{ns.sqs('bus')}"}}]
""",
    )
    result = apply(ws, "c.yaml")
    assert (result.returncode, result.stdout) == (0, f"+ rule:{ns.p}-r (create)\n"), result.stderr
    same(
        rule_snapshot(f"{ns.p}-r", bus=bus),
        {"rule": {"Name": f"{ns.p}-r", "State": "ENABLED", "EventPattern": {"source": ["y"]}}, "tags": {}, "targets": {"q": {"Arn": ns.sqs("bus")}}},
    )
    # The same-named rule on the default bus is untouched.
    same(
        rule_snapshot(f"{ns.p}-r"),
        {"rule": {"Name": f"{ns.p}-r", "State": "ENABLED", "ScheduleExpression": "rate(1 hour)"}, "tags": {}, "targets": {"q": {"Arn": ns.sqs()}}},
    )
    assert cli("-conf", "c.yaml", "diff", cwd=ws).returncode == 0
