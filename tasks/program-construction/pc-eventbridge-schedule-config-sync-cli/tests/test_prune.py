"""-prune: tracking-id scoped deletion of Rules and schedules, and its guards."""

from support import TRACKING_KEY, cli, events, create_group, group_tags, put_rule, put_schedule, rule_names, rule_snapshot, schedule_names, write


def rule(ns, name, expression="rate(1 hour)"):
    return f"""  - name: {name}
    scheduleExpression: {expression}
    state: ENABLED
    targets:
      - {{id: q, arn: "{ns.sqs()}"}}
"""


def schedule(ns, name, group=None):
    override = f"    groupName: {group}\n" if group else ""
    return f"""  - name: {name}
{override}    scheduleExpression: rate(1 hour)
    state: ENABLED
    flexibleTimeWindow: {{mode: "OFF"}}
    target: {{arn: "{ns.sqs()}", roleArn: "{ns.role()}"}}
"""


def test_rule_prune_is_scoped_by_tracking_id(ws, ns):
    p = ns.p
    write(ws, "full.yaml", f"region: us-east-1\ntrackingId: {ns.tracking}\nrules:\n" + rule(ns, f"{p}-keep") + rule(ns, f"{p}-drop") + rule(ns, f"{p}-drop2"))
    assert cli("-conf", "full.yaml", "-auto-approve", "apply", cwd=ws).returncode == 0
    put_rule(f"{p}-untracked", targets=[{"Id": "q", "Arn": ns.sqs()}])
    put_rule(f"{p}-foreign", tags={TRACKING_KEY: "someone-else"})
    put_rule(f"{p}-spoof", tags={"Owner": ns.tracking})
    write(ws, "less.yaml", f"region: us-east-1\ntrackingId: {ns.tracking}\nrules:\n" + rule(ns, f"{p}-keep", "rate(2 hours)"))
    everything = [f"{p}-drop", f"{p}-drop2", f"{p}-foreign", f"{p}-keep", f"{p}-spoof", f"{p}-untracked"]

    dry = cli("-conf", "less.yaml", "-dry-run", "-prune", "apply", cwd=ws)
    assert dry.returncode == 0, dry.stderr
    assert dry.stdout == f"~ rule:{p}-keep (update)\n- rule:{p}-drop (delete)\n- rule:{p}-drop2 (delete)\n"
    assert rule_names(p) == everything
    assert rule_snapshot(f"{p}-keep")["rule"]["ScheduleExpression"] == "rate(1 hour)"

    pruned = cli("-conf", "less.yaml", "-auto-approve", "-prune", "apply", cwd=ws)
    assert pruned.returncode == 0, pruned.stderr
    assert pruned.stdout == f"~ rule:{p}-keep (update)\n- rule:{p}-drop (delete)\n- rule:{p}-drop2 (delete)\n"
    assert rule_names(p) == [f"{p}-foreign", f"{p}-keep", f"{p}-spoof", f"{p}-untracked"]
    assert rule_snapshot(f"{p}-keep")["rule"]["ScheduleExpression"] == "rate(2 hours)"
    assert rule_snapshot(f"{p}-untracked")["targets"] == {"q": {"Arn": ns.sqs()}}

    again = cli("-conf", "less.yaml", "-auto-approve", "-prune", "apply", cwd=ws)
    assert (again.returncode, again.stdout) == (0, f"= rule:{p}-keep (no-op)\n")


def test_schedule_prune_walks_every_tracked_group(ws, ns):
    p = ns.p
    main, side, other, foreign, untracked = (f"{p}-{suffix}" for suffix in ("main", "side", "other", "foreign", "untracked"))
    create_group(other, {TRACKING_KEY: ns.tracking})
    create_group(foreign, {TRACKING_KEY: "someone-else"})
    create_group(untracked, {"Owner": "x"})
    for group in (other, foreign, untracked):
        put_schedule(group, f"{p}-pre-{group[-5:]}", ns.sqs(), ns.role())
    put_schedule("default", f"{p}-in-default", ns.sqs(), ns.role())
    write(
        ws,
        "full.yaml",
        f"region: us-east-1\ntrackingId: {ns.tracking}\ngroupName: {main}\nschedules:\n"
        + schedule(ns, f"{p}-a")
        + schedule(ns, f"{p}-b")
        + schedule(ns, f"{p}-c", side)
        + schedule(ns, f"{p}-d", untracked),
    )
    assert cli("-conf", "full.yaml", "-auto-approve", "apply", cwd=ws).returncode == 0
    write(ws, "less.yaml", f"region: us-east-1\ntrackingId: {ns.tracking}\ngroupName: {main}\nschedules:\n" + schedule(ns, f"{p}-a"))

    dry = cli("-conf", "less.yaml", "-dry-run", "-prune", "apply", cwd=ws)
    assert dry.returncode == 0, dry.stderr
    expected = [
        f"= schedule:{p}-a (no-op)",
        f"- schedule:{p}-b (delete from {main})",
        f"- schedule:{p}-pre-other (delete from {other})",
        f"- schedule:{p}-c (delete from {side})",
    ]
    assert dry.stdout.splitlines() == expected
    assert schedule_names(main) == [f"{p}-a", f"{p}-b"]
    assert schedule_names(other) == [f"{p}-pre-other"]

    pruned = cli("-conf", "less.yaml", "-auto-approve", "-prune", "apply", cwd=ws)
    assert pruned.returncode == 0, pruned.stderr
    assert pruned.stdout.splitlines() == expected
    assert schedule_names(main) == [f"{p}-a"]
    assert schedule_names(side) == []
    assert schedule_names(other) == []
    assert schedule_names(foreign) == [f"{p}-pre-reign"]
    assert schedule_names(untracked) == [f"{p}-d", f"{p}-pre-acked"]
    assert schedule_names("default", p) == [f"{p}-in-default"]
    # Groups themselves are never deleted.
    assert group_tags(side) == {TRACKING_KEY: ns.tracking}
    assert group_tags(other) == {TRACKING_KEY: ns.tracking}


def test_omitted_and_empty_sections(ws, ns):
    p, group = ns.p, f"{ns.p}-g"
    write(
        ws,
        "full.yaml",
        f"region: us-east-1\ntrackingId: {ns.tracking}\ngroupName: {group}\nrules:\n" + rule(ns, f"{p}-r") + "schedules:\n" + schedule(ns, f"{p}-s"),
    )
    assert cli("-conf", "full.yaml", "-auto-approve", "apply", cwd=ws).returncode == 0

    # rules omitted: Rules are not managed, so nothing of them is pruned; schedules: [] prunes the group.
    write(ws, "no-rules.yaml", f"region: us-east-1\ntrackingId: {ns.tracking}\ngroupName: {group}\nschedules: []\n")
    result = cli("-conf", "no-rules.yaml", "-auto-approve", "-prune", "apply", cwd=ws)
    assert (result.returncode, result.stdout) == (0, f"- schedule:{p}-s (delete from {group})\n"), result.stderr
    assert rule_names(p) == [f"{p}-r"]
    assert schedule_names(group) == []

    # schedules omitted, rules: [] prunes every tracked rule.
    assert cli("-conf", "full.yaml", "-auto-approve", "apply", cwd=ws).returncode == 0
    write(ws, "no-schedules.yaml", f"region: us-east-1\ntrackingId: {ns.tracking}\ngroupName: {group}\nrules: []\n")
    result = cli("-conf", "no-schedules.yaml", "-auto-approve", "-prune", "apply", cwd=ws)
    assert (result.returncode, result.stdout) == (0, f"- rule:{p}-r (delete)\n"), result.stderr
    assert rule_names(p) == []
    assert schedule_names(group) == [f"{p}-s"]

    # Without -prune an empty section changes nothing.
    write(ws, "empty.yaml", f"region: us-east-1\ntrackingId: {ns.tracking}\ngroupName: {group}\nrules: []\nschedules: []\n")
    result = cli("-conf", "empty.yaml", "-auto-approve", "apply", cwd=ws)
    assert (result.returncode, result.stdout) == (0, "")
    assert schedule_names(group) == [f"{p}-s"]


def test_prune_requires_tracking_id_after_applying(ws, ns):
    p = ns.p
    write(ws, "tracked.yaml", f"region: us-east-1\ntrackingId: {ns.tracking}\nrules:\n" + rule(ns, f"{p}-old"))
    assert cli("-conf", "tracked.yaml", "-auto-approve", "apply", cwd=ws).returncode == 0
    write(ws, "untracked.yaml", "region: us-east-1\nrules:\n" + rule(ns, f"{p}-new"))
    result = cli("-conf", "untracked.yaml", "-auto-approve", "-prune", "apply", cwd=ws)
    assert result.returncode == 1
    assert result.stdout == f"+ rule:{p}-new (create)\n"
    assert result.stderr == "error: -prune requires trackingId in config (safety guard)\n"
    assert rule_names(p) == [f"{p}-new", f"{p}-old"]

    group = f"{p}-g"
    write(ws, "sched.yaml", f"region: us-east-1\ngroupName: {group}\nschedules:\n" + schedule(ns, f"{p}-s"))
    result = cli("-conf", "sched.yaml", "-auto-approve", "-prune", "apply", cwd=ws)
    assert result.returncode == 1
    assert result.stdout == f"+ schedule-group:{group} (create)\n+ schedule:{p}-s (create)\n"
    assert result.stderr == "error: -prune requires trackingId in config (safety guard)\n"
    assert schedule_names(group) == [f"{p}-s"]


def test_prune_per_file_tracking_ids(ws, ns):
    p = ns.p
    write(ws, "conf/a.yaml", f"region: us-east-1\ntrackingId: {ns.tracking}-a\nrules:\n" + rule(ns, f"{p}-a1") + rule(ns, f"{p}-a2"))
    write(ws, "conf/b.yaml", f"region: us-east-1\ntrackingId: {ns.tracking}-b\nrules:\n" + rule(ns, f"{p}-b1"))
    result = cli("-conf", "conf/*.yaml", "-auto-approve", "apply", cwd=ws)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"# conf/a.yaml\n+ rule:{p}-a1 (create)\n+ rule:{p}-a2 (create)\n# conf/b.yaml\n+ rule:{p}-b1 (create)\n"
    assert rule_snapshot(f"{p}-b1")["tags"] == {TRACKING_KEY: f"{ns.tracking}-b"}

    write(ws, "conf/a.yaml", f"region: us-east-1\ntrackingId: {ns.tracking}-a\nrules:\n" + rule(ns, f"{p}-a1"))
    result = cli("-conf", "conf/*.yaml", "-auto-approve", "-prune", "apply", cwd=ws)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"# conf/a.yaml\n= rule:{p}-a1 (no-op)\n- rule:{p}-a2 (delete)\n# conf/b.yaml\n= rule:{p}-b1 (no-op)\n"
    assert result.stderr == "applied 2 config file(s)\n"
    assert rule_names(p) == [f"{p}-a1", f"{p}-b1"]


def test_rule_prune_stays_on_the_configured_bus(ws, ns):
    p, bus = ns.p, f"{ns.p}-bus"
    events().create_event_bus(Name=bus)
    tracked = {TRACKING_KEY: ns.tracking}
    put_rule(f"{p}-on-default", tags=tracked, targets=[{"Id": "q", "Arn": ns.sqs()}])
    put_rule(f"{p}-on-bus", bus=bus, tags=tracked, targets=[{"Id": "q", "Arn": ns.sqs()}])
    put_rule(f"{p}-other-on-bus", bus=bus, tags={TRACKING_KEY: "someone-else"})
    write(ws, "c.yaml", f"region: us-east-1\ntrackingId: {ns.tracking}\neventBusName: {bus}\nrules: []\n")
    result = cli("-conf", "c.yaml", "-auto-approve", "-prune", "apply", cwd=ws)
    assert (result.returncode, result.stdout) == (0, f"- rule:{p}-on-bus (delete)\n"), result.stderr
    assert rule_names(p, bus=bus) == [f"{p}-other-on-bus"]
    assert rule_names(p) == [f"{p}-on-default"]
