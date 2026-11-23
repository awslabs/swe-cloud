"""Configuration loading against AWS: templates, SSM, Jsonnet, baseFile and several files."""

import boto3
from support import TRACKING_KEY, assert_diff, changed_lines, cli, group_tags, rule_names, rule_snapshot, same, schedule_snapshot, write


def apply(ws, conf, *flags, env=None):
    return cli("-conf", conf, *flags, "-auto-approve", "apply", cwd=ws, env=env)


def parameters(ns):
    ssm = boto3.client("ssm")
    ssm.put_parameter(Name=f"/{ns.p}/str", Value="hello", Type="String")
    ssm.put_parameter(Name=f"/{ns.p}/list", Value="a,b,c", Type="StringList")
    ssm.put_parameter(Name=f"/{ns.p}/secret", Value="s3cr3t", Type="SecureString")


def test_template_functions(ws, ns):
    parameters(ns)
    write(
        ws,
        "t.yaml",
        f"""region: us-east-1
trackingId: {ns.tracking}
tags:
  Team: '{{{{ env "MY_TEAM" }}}}'
  Empty: '{{{{ env "UNSET_VAR_ZZ" }}}}'
  Second: '{{{{ ssm "/{ns.p}/list" 1 }}}}'
  Secret: '{{{{ ssm "/{ns.p}/secret" }}}}'
rules:
  - name: {ns.p}-t
    description: '{{{{ ssm "/{ns.p}/list" }}}}'
    scheduleExpression: rate(1 hour)
    state: ENABLED
    targets:
      - id: q
        arn: arn:aws:sqs:us-east-1:{{{{ must_env "AWS_ACCOUNT_ID" }}}}:{ns.p}-q
        input: {{v: '{{{{ ssm "/{ns.p}/str" }}}}', team: '{{{{ must_env "MY_TEAM" }}}}'}}
{{{{- if env "MY_TEAM" }}}}
  - name: {ns.p}-conditional
    scheduleExpression: rate(2 hours)
    state: ENABLED
    targets: [{{id: q, arn: "{ns.sqs()}"}}]
{{{{- end }}}}
""",
    )
    result = apply(ws, "t.yaml", env={"MY_TEAM": "blue"})
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"+ rule:{ns.p}-t (create)\n+ rule:{ns.p}-conditional (create)\n"
    same(
        rule_snapshot(f"{ns.p}-t"),
        {
            "rule": {"Name": f"{ns.p}-t", "State": "ENABLED", "Description": "a,b,c", "ScheduleExpression": "rate(1 hour)"},
            "tags": {"Team": "blue", "Empty": "", "Second": "b", "Secret": "s3cr3t", TRACKING_KEY: ns.tracking},
            "targets": {"q": {"Arn": ns.sqs(), "Input": {"v": "hello", "team": "blue"}}},
        },
    )
    clean = cli("-conf", "t.yaml", "diff", cwd=ws, env={"MY_TEAM": "blue"})
    assert (clean.returncode, clean.stdout) == (0, "")

    # An explicit AWS_ACCOUNT_ID wins over the caller identity.
    other = cli("-conf", "t.yaml", "diff", cwd=ws, env={"MY_TEAM": "blue", "AWS_ACCOUNT_ID": "111122223333"})
    assert other.returncode == 2
    assert_diff(changed_lines(other.stdout), f"-    arn: {ns.sqs()}\n+    arn: arn:aws:sqs:us-east-1:111122223333:{ns.p}-q\n")


def test_template_failures_stop_before_any_change(ws, ns):
    parameters(ns)
    head = f"region: us-east-1\nrules:\n  - name: {ns.p}-x\n    scheduleExpression: rate(1 hour)\n    state: ENABLED\n"
    cases = {
        "must.yaml": head + '    description: {{ must_env "UNSET_VAR_ZZ" }}\n    targets: [{id: a, arn: "arn:x"}]\n',
        "index.yaml": head + f'    description: {{{{ ssm "/{ns.p}/list" 3 }}}}\n    targets: [{{id: a, arn: "arn:x"}}]\n',
        "missing.yaml": head + f'    description: {{{{ ssm "/{ns.p}/none" }}}}\n    targets: [{{id: a, arn: "arn:x"}}]\n',
        "syntax.yaml": head + '    description: {{ env "X" \n    targets: [{id: a, arn: "arn:x"}]\n',
        "unknown.yaml": head + '    description: {{ nosuchfunc "X" }}\n    targets: [{id: a, arn: "arn:x"}]\n',
    }
    for name, text in cases.items():
        write(ws, name, text)
        for args in (["diff"], ["-auto-approve", "apply"], ["dump", ns.p]):
            result = cli("-conf", name, *args, cwd=ws)
            assert result.returncode == 1, (name, args, result.stderr)
            assert result.stdout == ""
            assert result.stderr.startswith(f"error: {name}: "), result.stderr
    assert rule_names(ns.p) == []
    # The template functions' own messages appear; the template engine's location wrapping is not graded.
    assert "env var UNSET_VAR_ZZ is not set" in cli("-conf", "must.yaml", "diff", cwd=ws).stderr
    assert f"ssm:/{ns.p}/list: index 3 out of range (len=3)" in cli("-conf", "index.yaml", "diff", cwd=ws).stderr


def test_jsonnet_configuration(ws, ns):
    parameters(ns)
    write(ws, "j/lib.libsonnet", "{ target(acct, name): { id: 'q', arn: 'arn:aws:sqs:us-east-1:%s:%s' % [acct, name] } }\n")
    write(
        ws,
        "j/conf.jsonnet",
        f"""local lib = import 'lib.libsonnet';
local acct = std.native('must_env')('AWS_ACCOUNT_ID');
{{
  region: std.native('env')('UNSET_REGION_ZZ', 'us-east-1'),
  trackingId: '{ns.tracking}',
  groupName: '{ns.p}-g',
  tags: {{
    S: std.native('ssm')('/{ns.p}/str'),
    L: std.join('+', std.native('ssmList')('/{ns.p}/list')),
    T: std.native('env')('MY_TEAM', 'none'),
  }},
  rules: [
    {{ name: '{ns.p}-j%d' % i, scheduleExpression: 'rate(%d hours)' % (i + 1), state: 'ENABLED',
       targets: [lib.target(acct, '{ns.p}-q') {{ input: {{ n: i, f: 1.5, ok: true }} }}] }}
    for i in [1, 2]
  ],
  schedules: [
    {{ name: '{ns.p}-js', scheduleExpression: 'cron(0 3 * * ? *)', state: 'ENABLED',
       flexibleTimeWindow: {{ mode: 'OFF' }},
       target: {{ arn: lib.target(acct, '{ns.p}-q').arn, roleArn: '{ns.role()}' }} }},
  ],
}}
""",
    )
    result = apply(ws, "j/conf.jsonnet", env={"MY_TEAM": "green"})
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        f"+ rule:{ns.p}-j1 (create)",
        f"+ rule:{ns.p}-j2 (create)",
        f"+ schedule-group:{ns.p}-g (create)",
        f"+ schedule:{ns.p}-js (create)",
    ]
    tags = {"S": "hello", "L": "a+b+c", "T": "green", TRACKING_KEY: ns.tracking}
    for i in (1, 2):
        same(
            rule_snapshot(f"{ns.p}-j{i}"),
            {
                "rule": {"Name": f"{ns.p}-j{i}", "State": "ENABLED", "ScheduleExpression": f"rate({i + 1} hours)"},
                "tags": tags,
                "targets": {"q": {"Arn": ns.sqs(), "Input": {"n": i, "f": 1.5, "ok": True}}},
            },
        )
    assert group_tags(f"{ns.p}-g") == tags
    assert schedule_snapshot(f"{ns.p}-g", f"{ns.p}-js")["Target"] == {"Arn": ns.sqs(), "RoleArn": ns.role()}
    clean = cli("-conf", "j/conf.jsonnet", "diff", cwd=ws, env={"MY_TEAM": "green"})
    assert (clean.returncode, clean.stdout) == (0, "")

    write(ws, "j/fail.jsonnet", "{ rules: [], region: std.native('must_env')('UNSET_VAR_ZZ') }\n")
    failed = cli("-conf", "j/fail.jsonnet", "diff", cwd=ws)
    assert failed.returncode == 1 and failed.stderr.startswith("error: j/fail.jsonnet: ") and "env var UNSET_VAR_ZZ is not set" in failed.stderr


def test_base_file_inheritance(ws, ns):
    write(ws, "root.yaml", f"groupName: {ns.p}-rootgrp\ntags: {{Root: r, Env: root}}\n")
    write(ws, "sub/_base.yaml", f"region: us-east-1\ntrackingId: {ns.tracking}\ntags: {{Service: s, Env: base}}\nbaseFile: ../root.yaml\n")
    write(
        ws,
        "sub/app.yaml",
        f"""baseFile: _base.yaml
tags: {{Env: app}}
rules:
  - name: {ns.p}-br
    scheduleExpression: rate(1 hour)
    state: ENABLED
    tags: {{Root: rule}}
    targets: [{{id: a, arn: "{ns.sqs()}"}}]
schedules:
  - name: {ns.p}-bs
    scheduleExpression: rate(1 hour)
    state: ENABLED
    flexibleTimeWindow: {{mode: "OFF"}}
    target: {{arn: "{ns.sqs()}", roleArn: "{ns.role()}"}}
""",
    )
    result = apply(ws, "sub/app.yaml")
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"+ rule:{ns.p}-br (create)\n+ schedule-group:{ns.p}-rootgrp (create)\n+ schedule:{ns.p}-bs (create)\n"
    assert rule_snapshot(f"{ns.p}-br")["tags"] == {"Service": "s", "Env": "app", "Root": "rule", TRACKING_KEY: ns.tracking}
    assert group_tags(f"{ns.p}-rootgrp") == {"Service": "s", "Env": "app", "Root": "r", TRACKING_KEY: ns.tracking}
    # The inherited trackingId makes -prune possible from the child file.
    write(ws, "sub/empty.yaml", "baseFile: _base.yaml\nrules: []\nschedules: []\n")
    pruned = apply(ws, "sub/empty.yaml", "-prune")
    assert pruned.returncode == 0, pruned.stderr
    assert pruned.stdout == f"- rule:{ns.p}-br (delete)\n- schedule:{ns.p}-bs (delete from {ns.p}-rootgrp)\n"


def test_multi_file_apply_stops_at_failing_file(ws, ns):
    write(ws, "m/01-a.yaml", f"region: us-east-1\nrules:\n  - name: {ns.p}-a\n    scheduleExpression: rate(1 hour)\n    state: ENABLED\n    targets: [{{id: q, arn: \"{ns.sqs()}\"}}]\n")
    write(ws, "m/02-empty.yaml", "")
    write(
        ws,
        "m/03-bad.yaml",
        f"""region: us-east-1
rules:
  - name: {ns.p}-b
    scheduleExpression: rate(1 hour)
    state: ENABLED
    targets: [{{id: q, arn: "{ns.sqs()}"}}]
schedules:
  - name: {ns.p}-s
    groupName: "bad group!"
    scheduleExpression: rate(1 hour)
    target: {{arn: "{ns.sqs()}", roleArn: "{ns.role()}"}}
""",
    )
    write(ws, "m/04-c.yaml", f"region: us-east-1\nrules:\n  - name: {ns.p}-c\n    scheduleExpression: rate(1 hour)\n    state: ENABLED\n    targets: [{{id: q, arn: \"{ns.sqs()}\"}}]\n")
    result = apply(ws, "m/*.yaml")
    assert result.returncode == 1
    assert result.stdout == f"# m/01-a.yaml\n+ rule:{ns.p}-a (create)\n# m/03-bad.yaml\n+ rule:{ns.p}-b (create)\n"
    stderr = result.stderr.splitlines()
    assert stderr[0] == "applied 1 of 3 config file(s) before error in m/03-bad.yaml", result.stderr
    assert stderr[1].startswith("error: ensure schedule group bad group!: "), result.stderr
    assert rule_names(ns.p) == [f"{ns.p}-a", f"{ns.p}-b"]

    # A single-file failure prints no summary line.
    single = apply(ws, "m/03-bad.yaml")
    assert single.returncode == 1
    assert single.stdout == f"= rule:{ns.p}-b (no-op)\n"
    assert len(single.stderr.splitlines()) == 1 and single.stderr.startswith("error: ensure schedule group bad group!: ")
