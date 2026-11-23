"""Offline validation: problem reports, load-time errors and placeholder templating."""

from support import assert_problems, cli, write

# An unreachable endpoint: validate must not need AWS at all.
OFFLINE = {"AWS_ENDPOINT_URL": "http://127.0.0.1:9", "AWS_ACCESS_KEY_ID": "", "AWS_SECRET_ACCESS_KEY": ""}


def validate(ws, conf):
    return cli("-conf", conf, "validate", cwd=ws, env=OFFLINE)


def test_valid_file_with_placeholders_is_offline(ws):
    write(
        ws,
        "ok.yaml",
        """region: us-east-1
trackingId: '{{ env "UNSET_TRACKING_ZZ" }}x'
tags: {Owner: '{{ must_env "UNSET_OWNER_ZZ" }}', From: '{{ ssm "/team/list" 1 }}'}
rules:
  - name: ok.rule_1
    scheduleExpression: cron(0 18 ? * MON-FRI *)
    state: DISABLED
    targets:
      - id: a
        arn: '{{ must_env "UNSET_ARN_ZZ" }}'
      - id: b
        arn: '{{ ssm "/team/queue" }}'
        input: {msg: '{{ ssm "/team/msg" }}'}
schedules:
  - name: weekly
    scheduleExpression: at(2030-01-01T00:00:00)
    scheduleExpressionTimezone: Asia/Tokyo
    target: {arn: '{{ ssm "/team/fn" }}', roleArn: '{{ must_env "UNSET_ROLE_ZZ" }}'}
""",
    )
    result = validate(ws, "ok.yaml")
    assert (result.returncode, result.stdout, result.stderr) == (0, "OK: 1 config file(s) valid\n", "")


def test_rule_problems_are_all_reported(ws):
    write(
        ws,
        "rules.yaml",
        """tags: {"aws:bad": v, ebschedule-tracking-id: x}
rules:
  - name: "bad name!"
    scheduleExpression: rate(5 minutes)
    eventPattern: '{not json'
    state: PAUSED
    targets: []
  - name: ok
    scheduleExpression: rate(1 hour)
    tags: {"": e}
    targets:
      - {id: a, arn: "not-an-arn", input: "{bad", inputPath: "$.x"}
      - {id: a, arn: ""}
      - {arn: "arn:aws:sqs:us-east-1:1:q", inputTransformer: {inputTemplate: ""}, kinesisParameters: {partitionKeyPath: ""}}
  - name: ok
    targets: [{id: a, arn: "<ssm:/x>"}]
  - name: six
    eventPattern: {source: [a]}
    targets: [{id: "1", arn: "arn:1"}, {id: "2", arn: "arn:2"}, {id: "3", arn: "arn:3"}, {id: "4", arn: "arn:4"}, {id: "5", arn: "arn:5"}, {id: "6", arn: "arn:6"}]
  - name: five-is-fine
    eventPattern: '{"source": ["a"]}'
    targets: [{id: "1", arn: "arn:1"}, {id: "2", arn: "arn:2"}, {id: "3", arn: "arn:3"}, {id: "4", arn: "arn:4"}, {id: "5", arn: "<env:X>"}]
""",
    )
    result = validate(ws, "rules.yaml")
    assert result.returncode == 1 and result.stdout == ""
    assert_problems(
        result.stderr,
        "rules.yaml",
        [
            'tags: tag key "ebschedule-tracking-id" is reserved by ebschedule',
            'tags: tag key "aws:bad" cannot start with aws:',
            r"rule[0]:bad name!: name must match ^[A-Za-z0-9_.\-]+$ and be <=64 chars",
            "rule[0]:bad name!: scheduleExpression and eventPattern are mutually exclusive",
            "rule[0]:bad name!: eventPattern is not valid JSON",
            "rule[0]:bad name!: state must be ENABLED or DISABLED",
            "rule[0]:bad name!: at least one target is required",
            'rule[1]:ok.tags: tag key "" must be 1-128 chars',
            "rule[1]:ok.targets[0]:a: arn does not look like an ARN",
            "rule[1]:ok.targets[0]:a.input: not valid JSON",
            "rule[1]:ok.targets[0]:a: input, inputPath, and inputTransformer are mutually exclusive",
            "rule[1]:ok.targets[1]:a: duplicate id (also at targets[0])",
            "rule[1]:ok.targets[1]:a: arn is required",
            "rule[1]:ok.targets[2]:: target id is required",
            "rule[1]:ok.targets[2]:.inputTransformer.inputTemplate: is required",
            "rule[1]:ok.targets[2]:.kinesisParameters.partitionKeyPath: is required",
            "rule[2]:ok: duplicate name (also at rule[1])",
            "rule[2]:ok: must set either scheduleExpression or eventPattern",
            "rule[3]:six: a rule can have at most 5 targets (got 6)",
        ],
    )


def test_schedule_problems_are_all_reported(ws):
    write(
        ws,
        "schedules.yaml",
        """schedules:
  - name: s1
    scheduleExpression: rate(1 hour)
    scheduleExpressionTimezone: Mars/Olympus
    state: "on"
    actionAfterCompletion: KEEP
    startDate: "2030-01-02T00:00:00Z"
    endDate: "2030-01-01T00:00:00Z"
    flexibleTimeWindow: {mode: FLEXIBLE}
    target: {arn: "x", roleArn: "", input: "[1,"}
  - name: s2
    scheduleExpression: at(2030-01-01T00:00:00)
    startDate: yesterday
    flexibleTimeWindow: {mode: SOMETIMES}
  - name: ""
    scheduleExpression: rate(1 hour)
    target: {arn: "arn:x", roleArn: "r", kinesisParameters: {partitionKey: ""}}
  - name: s1
    scheduleExpression: cron(0 0 1 * ? *)
    state: DISABLED
    actionAfterCompletion: DELETE
    startDate: 2030-01-01T00:00:00+09:00
    endDate: 2030-01-01T00:00:01+09:00
    flexibleTimeWindow: {mode: OFF}
    target: {arn: "<env:X>", roleArn: "r"}
  - name: s5
    scheduleExpression: rate(1 hour)
    startDate: 2030-01-01T00:00:00Z
    endDate: 2030-01-01T00:00:00Z
    flexibleTimeWindow: {mode: FLEXIBLE, maximumWindowInMinutes: 1}
    target: {arn: "arn:x", roleArn: "r"}
""",
    )
    result = validate(ws, "schedules.yaml")
    assert result.returncode == 1 and result.stdout == ""
    assert_problems(
        result.stderr,
        "schedules.yaml",
        [
            'schedule[0]:s1.scheduleExpressionTimezone: invalid IANA timezone "Mars/Olympus"',
            "schedule[0]:s1.state: must be ENABLED or DISABLED",
            "schedule[0]:s1.actionAfterCompletion: must be NONE or DELETE",
            "schedule[0]:s1: endDate must be after startDate",
            "schedule[0]:s1.flexibleTimeWindow.maximumWindowInMinutes: must be > 0 when mode=FLEXIBLE",
            "schedule[0]:s1.target.arn: does not look like an ARN",
            "schedule[0]:s1.target.roleArn: is required",
            "schedule[0]:s1.target.input: not valid JSON",
            "schedule[1]:s2.startDate: must be RFC3339",
            "schedule[1]:s2.flexibleTimeWindow.mode: must be OFF or FLEXIBLE",
            "schedule[1]:s2.target: is required",
            "schedule[2]:: name is required",
            "schedule[2]:.target.kinesisParameters.partitionKey: is required",
            "schedule[3]:s1: duplicate name (also at schedule[0])",
            "schedule[4]:s5: endDate must be after startDate",
        ],
    )


def test_problems_counted_across_files(ws):
    write(ws, "conf/a.yaml", "rules:\n  - name: r\n    scheduleExpression: rate(1 hour)\n    targets: []\n")
    write(ws, "conf/b.yaml", "rules: []\nschedules:\n  - name: s\n    scheduleExpression: rate(1 hour)\n")
    write(ws, "conf/c.yaml", "schedules: []\n")
    result = validate(ws, "conf/*.yaml")
    assert result.returncode == 1 and result.stdout == ""
    got = result.stderr.splitlines()
    assert got[-1:] == ["error: validation failed: 2 problem(s)"], result.stderr
    assert sorted(got[:-1]) == [
        "conf/a.yaml: rule[0]:r: at least one target is required",
        "conf/b.yaml: schedule[0]:s.target: is required",
    ]


def test_file_counts_skip_empty_files(ws):
    write(ws, "m/00-empty.yaml", "  \n\n")
    write(ws, "m/01-rules.yaml", "rules: []\n")
    write(ws, "m/02-schedules.yaml", "schedules: []\n")
    write(ws, "m/03-empty.yaml", "")
    result = validate(ws, "m/*.yaml")
    assert (result.returncode, result.stdout, result.stderr) == (0, "OK: 2 config file(s) valid\n", "")
    empty = validate(ws, "m/0[03]-*.yaml")
    assert (empty.returncode, empty.stdout) == (0, "OK: 0 config file(s) valid\n")


def test_schedule_expressions_are_checked_at_load(ws):
    cases = {
        "shape.yaml": (
            "rules:\n  - name: bad name!\n    scheduleExpression: every 5 minutes\n    state: NOPE\n    targets: []\n",
            'error: shape.yaml: rules[bad name!]: scheduleExpression "every 5 minutes" must look like cron(...), rate(...) or at(...)',
        ),
        "space.yaml": (
            "rules:\n  - name: r\n    scheduleExpression: cron( 0 18 * * ? *)\n    targets: [{id: a, arn: arn:x}]\n",
            'error: space.yaml: rules[r]: scheduleExpression "cron( 0 18 * * ? *)" has stray whitespace inside cron(...)',
        ),
        "empty.yaml": (
            "schedules:\n  - name: s\n    target: {arn: arn:x, roleArn: r}\n",
            "error: empty.yaml: schedules[s]: scheduleExpression is empty",
        ),
    }
    for name, (text, message) in cases.items():
        write(ws, name, text)
        result = validate(ws, name)
        assert (result.returncode, result.stdout, result.stderr) == (1, "", message + "\n"), name


def test_malformed_cron_fields_are_rejected(ws):
    # The parser's own explanation after the quoted expression is not graded.
    bad = {
        "minute.yaml": ("schedules", "s", "cron(99 9 * * ? *)"),
        "fields.yaml": ("rules", "r", "cron(0 18 * * ?)"),
        "both.yaml": ("rules", "r", "cron(0 18 1 * MON *)"),
        "month.yaml": ("schedules", "s", "cron(0 0 1 13 ? *)"),
    }
    for name, (section, resource, expression) in bad.items():
        if section == "rules":
            write(ws, name, f"rules:\n  - name: {resource}\n    scheduleExpression: {expression}\n    targets: [{{id: a, arn: arn:x}}]\n")
        else:
            write(ws, name, f"schedules:\n  - name: {resource}\n    scheduleExpression: {expression}\n    target: {{arn: arn:x, roleArn: r}}\n")
        result = validate(ws, name)
        assert result.returncode == 1 and result.stdout == "", name
        stderr = result.stderr.splitlines()
        assert len(stderr) == 1, result.stderr
        assert stderr[0].startswith(f'error: {name}: {section}[{resource}]: scheduleExpression "{expression}": '), result.stderr
    good = ["cron(0 18 * * ? *)", "cron(0/15 8-17 ? * MON-FRI *)", "cron(0 0 L * ? 2030)", "cron(30 9 ? * 2#1 *)", "rate(1 minute)", "at(2031-12-31T23:59:59)"]
    body = "".join(f"  - name: g{i}\n    scheduleExpression: {expr}\n    target: {{arn: arn:x, roleArn: r}}\n" for i, expr in enumerate(good))
    write(ws, "good.yaml", "schedules:\n" + body)
    result = validate(ws, "good.yaml")
    assert (result.returncode, result.stdout, result.stderr) == (0, "OK: 1 config file(s) valid\n", "")


def test_structural_load_errors(ws):
    write(ws, "neither.yaml", "region: us-east-1\ntags: {a: b}\n")
    result = validate(ws, "neither.yaml")
    assert (result.returncode, result.stdout, result.stderr) == (1, "", "error: neither.yaml: neither rules: nor schedules: present\n")
    for name, text, field in (
        ("top.yaml", "region: us-east-1\nrulez: []\n", "rulez"),
        ("nested.yaml", "rules:\n  - name: r\n    scheduleExpression: rate(1 hour)\n    bogus: 1\n    targets: [{id: a, arn: arn:x}]\n", "bogus"),
        ("target.yaml", "schedules:\n  - name: s\n    scheduleExpression: rate(1 hour)\n    target: {arn: arn:x, roleArn: r, tags: {a: b}}\n", "tags"),
    ):
        write(ws, name, text)
        result = validate(ws, name)
        assert result.returncode == 1 and result.stdout == "", name
        assert result.stderr.startswith(f"error: {name}: "), result.stderr
        assert field in result.stderr, result.stderr
    write(ws, "tmpl.yaml", 'rules: {{ env "X" \n')
    result = validate(ws, "tmpl.yaml")
    assert result.returncode == 1 and result.stderr.startswith("error: tmpl.yaml: "), result.stderr


def test_base_file_errors(ws):
    write(ws, "b/loop1.yaml", "baseFile: loop2.yaml\n")
    write(ws, "b/loop2.yaml", "baseFile: loop1.yaml\n")
    write(ws, "b/useloop.yaml", "baseFile: loop1.yaml\nrules: []\n")
    write(ws, "b/withrules.yaml", "region: us-east-1\nrules:\n  - name: x\n    scheduleExpression: rate(1 hour)\n    targets: [{id: a, arn: arn:x}]\n")
    write(ws, "b/usewr.yaml", "baseFile: withrules.yaml\nrules: []\n")
    write(ws, "b/usenope.yaml", "baseFile: nope.yaml\nrules: []\n")
    expected = {
        "b/useloop.yaml": f"error: b/useloop.yaml: baseFile cycle detected at {ws}/b/loop1.yaml",
        "b/usewr.yaml": f"error: b/usewr.yaml: baseFile {ws}/b/withrules.yaml: must not declare rules: or schedules: (those belong in the child file)",
        "b/usenope.yaml": f"error: b/usenope.yaml: baseFile {ws}/b/nope.yaml: ",
    }
    for conf, message in expected.items():
        result = validate(ws, conf)
        assert result.returncode == 1 and result.stdout == "", conf
        assert result.stderr.startswith(message), result.stderr
        assert len(result.stderr.splitlines()) == 1, result.stderr


def test_jsonnet_validation(ws):
    write(ws, "j/lib.libsonnet", "{ target(acct): { id: 'q', arn: 'arn:aws:sqs:us-east-1:%s:q' % acct } }\n")
    write(
        ws,
        "j/conf.jsonnet",
        """local lib = import 'lib.libsonnet';
local acct = std.native('must_env')('UNSET_ACCT_ZZ');
{
  region: std.native('env')('UNSET_REGION_ZZ', 'us-east-1'),
  tags: { S: std.native('ssm')('/p/s'), L: std.join('+', std.native('ssmList')('/p/l')) },
  rules: [{ name: 'jr', scheduleExpression: 'rate(1 hour)', targets: [lib.target(acct)] }],
}
""",
    )
    result = validate(ws, "j/conf.jsonnet")
    assert (result.returncode, result.stdout, result.stderr) == (0, "OK: 1 config file(s) valid\n", "")

    write(ws, "j/bad.jsonnet", "{ rules: [ { name: 'x', scheduleExpression: 'rate(1 hour)', targets: [{id: 'a', arn: 'nope'}] } ] }\n")
    result = validate(ws, "j/bad.jsonnet")
    assert result.returncode == 1
    assert_problems(result.stderr, "j/bad.jsonnet", ["rule[0]:x.targets[0]:a: arn does not look like an ARN"])

    write(ws, "j/unknown.jsonnet", "{ rules: [ { name: 'x', bogus: 1 } ] }\n")
    result = validate(ws, "j/unknown.jsonnet")
    assert result.returncode == 1 and result.stderr.startswith("error: j/unknown.jsonnet: ") and "bogus" in result.stderr
