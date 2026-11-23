"""Multi-entry and multi-step sequences: lifecycle, mixed plans, filtering, confirmation, import-edit-apply."""

from collections import Counter

from conftest import (
    SCHEDULER_ROLE,
    assert_report,
    assert_stdout,
    changes,
    diff_lines,
    schedule,
    state,
    wait_definition,
)


def test_lifecycle_of_two_entries(env):
    group = env.group()
    a, b = env.name("life-a"), env.name("life-b")
    sched = env.schedule_name("life-sch", group)
    entry_a = {"state": state(a, tags=[("app", "a")]), "schedule": schedule(env, group, sched, a)}
    entry_b = {"state": state(b, definition=wait_definition(1), tags=[("app", "b")])}

    env.write("c.jsonnet", [entry_a, entry_b])
    proc, report = env.report("plan", "c.jsonnet")
    assert_report(report, [(a, ["create_state", "create_schedule"]), (b, ["create_state"])])
    assert_stdout(proc, report, unchanged_entries=0)
    assert env.describe(a) is None and env.describe(b) is None and env.schedule(group, sched) is None

    env.report("apply", "c.jsonnet", "-a")
    assert env.machine(a)["tags"] == Counter({("app", "a"): 1})
    assert env.machine(b)["definition"] == wait_definition(1)
    assert env.schedule(group, sched)["Target"]["Arn"] == env.arn(a)
    proc, report = env.report("plan", "c.jsonnet")
    assert_report(report, [])
    assert_stdout(proc, report, unchanged_entries=2)

    # change a's schedule and b's tags in one apply
    machine_a, definition_b = env.machine(a), env.machine(b)["definition"]
    entry_a2 = {**entry_a, "schedule": {**entry_a["schedule"], "scheduleExpression": "rate(2 hours)"}}
    entry_b2 = {"state": state(b, definition=wait_definition(1), tags=[("app", "b2")])}
    env.write("c.jsonnet", [entry_a2, entry_b2])
    proc, report = env.report("apply", "c.jsonnet", "-a")
    assert_report(report, [(a, ["update_schedule"]), (b, ["update_state"])])
    assert env.schedule(group, sched)["ScheduleExpression"] == "rate(2 hours)"
    assert env.machine(a) == machine_a
    assert env.machine(b)["tags"] == Counter({("app", "b2"): 1}) and env.machine(b)["definition"] == definition_b

    # drop a's schedule, then everything
    env.write("c.jsonnet", [{**entry_a2, "deleteSchedule": True}, entry_b2])
    _, report = env.report("apply", "c.jsonnet", "-a")
    assert_report(report, [(a, ["delete_schedule"])])
    assert env.schedule(group, sched) is None and env.machine(a) == machine_a and env.machine(b) is not None

    env.write("c.jsonnet", [{**entry_a2, "deleteAll": True}, {**entry_b2, "deleteAll": True}])
    _, report = env.report("apply", "c.jsonnet", "-a")
    assert_report(report, [(a, ["delete_state"]), (b, ["delete_state"])])
    assert env.machine(a) is None and env.machine(b) is None
    proc, report = env.report("plan", "c.jsonnet")
    assert_report(report, [])
    assert_stdout(proc, report, unchanged_entries=2)


def test_mixed_plan_order_summary_and_targeted_apply(env):
    group = env.group()
    new, upd, same, gone = env.name("new"), env.name("upd"), env.name("same"), env.name("gone")
    new_sched = env.schedule_name("new-sch", group)
    gone_sched = env.schedule_name("gone-sch", group)
    initial = [
        {"state": state(upd, definition=wait_definition(1))},
        {"state": state(same)},
        {"state": state(gone), "schedule": schedule(env, group, gone_sched, gone)},
    ]
    env.write("setup.jsonnet", initial)
    env.report("apply", "setup.jsonnet", "-a")
    snapshot = {name: env.machine(name) for name in (upd, same, gone)}
    gone_schedule = env.schedule(group, gone_sched)

    config = [
        {"state": state(new), "schedule": schedule(env, group, new_sched, new)},
        {"state": state(upd, definition=wait_definition(2))},
        {"state": state(same)},
        {"state": state(gone), "schedule": schedule(env, group, gone_sched, gone), "deleteAll": True},
    ]
    env.write("c.jsonnet", config)
    proc, report = env.report("plan", "c.jsonnet")
    expected = [(new, ["create_state", "create_schedule"]), (upd, ["update_state"]), (gone, ["delete_schedule", "delete_state"])]
    assert_report(report, expected)
    assert_stdout(proc, report, unchanged_entries=1)
    assert [diff_lines(text)[0] for text in report["text_diff"][:2]] == [[(new, new), (new_sched, new_sched)], [(upd, upd)]]
    _, removed, added = diff_lines(report["text_diff"][1])
    assert (removed, added) == changes(("-", '        "Seconds": 1,'), ("+", '        "Seconds": 2,'))
    assert [line for line in report["text_diff"][2].split("\n") if line] == [f"State machine({gone}) is going to be deleted", f"Schedule({gone_sched}) is going to be deleted"]

    # targeted apply only touches the selected entries
    proc, report = env.report("apply", "c.jsonnet", "-a", "-t", upd, "-t", same)
    assert_report(report, [(upd, ["update_state"])])
    assert_stdout(proc, report, unchanged_entries=1)
    assert env.machine(upd)["definition"] == wait_definition(2)
    assert env.describe(new) is None and env.schedule(group, new_sched) is None
    assert env.machine(gone) == snapshot[gone] and env.schedule(group, gone_sched) == gone_schedule
    assert env.machine(same) == snapshot[same]

    _, report = env.report("apply", "c.jsonnet", "-a")
    assert_report(report, [(new, ["create_state", "create_schedule"]), (gone, ["delete_schedule", "delete_state"])])
    assert env.machine(new) is not None and env.schedule(group, new_sched) is not None
    assert env.machine(gone) is None and env.schedule(group, gone_sched) is None
    assert env.machine(same) == snapshot[same]
    _, report = env.report("plan", "c.jsonnet")
    assert_report(report, [])


def test_cancelled_update_then_approved(env):
    group = env.group()
    machine = env.name("confirm")
    sched = env.schedule_name("confirm-sch", group)
    base = {"state": state(machine, tags=[("v", "1")]), "schedule": schedule(env, group, sched, machine)}
    env.write("v1.jsonnet", [base])
    env.report("apply", "v1.jsonnet", "-a")
    machine_before, schedule_before = env.machine(machine), env.schedule(group, sched)

    env.write("v2.jsonnet", [{"state": state(machine, definition=wait_definition(6), tags=[("v", "2")]),
                              "schedule": {**base["schedule"], "state": "DISABLED"}}])
    proc, report = env.report("apply", "v2.jsonnet", stdin="nope\n", fails=True)
    assert "apply cancelled!" in proc.stderr
    assert_report(report, [(machine, ["update_state", "update_schedule"])])
    assert env.machine(machine) == machine_before and env.schedule(group, sched) == schedule_before

    proc, report = env.report("apply", "v2.jsonnet", stdin="yes\n")
    assert_report(report, [(machine, ["update_state", "update_schedule"])])
    found = env.machine(machine)
    assert found["definition"] == wait_definition(6) and found["tags"] == Counter({("v", "2"): 1})
    assert env.schedule(group, sched)["State"] == "DISABLED"


def test_import_edit_with_jsonnet_and_apply(env):
    group = env.group()
    machine = env.name("edit")
    sched = env.schedule_name("edit-sch", group)
    env.sfn.create_state_machine(name=machine, definition='{"StartAt":"S","States":{"S":{"Type":"Succeed"}}}', roleArn="arn:aws:iam::123456789012:role/legacy",
                                 tags=[{"key": "owner", "value": "legacy"}])
    env.scheduler.create_schedule(GroupName=group, Name=sched, State="ENABLED", ScheduleExpression="rate(1 day)",
                                  FlexibleTimeWindow={"Mode": "OFF"}, Target={"Arn": env.arn(machine), "RoleArn": SCHEDULER_ROLE})
    proc = env.run("import", "-c", "imported.json", "-f", machine, "-s", f"{group}/{sched}")
    assert proc.returncode == 0, proc.stderr

    env.write("edit.jsonnet", """
local imported = import 'imported.json';
[
  imported[0] + {
    state+: { tags: [{ key: 'owner', value: std.extVar('owner') }] },
    schedule+: { scheduleExpression: 'rate(12 hours)', target+: { input: '{"source": "edited"}' } },
  },
]
""")
    proc, report = env.report("plan", "edit.jsonnet", "-V", "owner=platform")
    assert_report(report, [(machine, ["update_state", "update_schedule"])])
    _, removed, added = diff_lines(report["text_diff"][0])
    assert (removed, added) == changes(
        ("-", '      "value": "legacy"'), ("+", '      "value": "platform"'),
        ("-", '  "scheduleExpression": "rate(1 day)",'), ("+", '  "scheduleExpression": "rate(12 hours)",'),
        ("-", '    "input": null,'), ("+", '    "input": "{\\"source\\": \\"edited\\"}",'),
    )

    env.report("apply", "edit.jsonnet", "-V", "owner=platform", "-a")
    found = env.machine(machine)
    assert found["tags"] == Counter({("owner", "platform"): 1}) and found["roleArn"] == "arn:aws:iam::123456789012:role/legacy"
    remote = env.schedule(group, sched)
    assert remote["ScheduleExpression"] == "rate(12 hours)" and remote["Target"]["Input"] == '{"source": "edited"}'
    _, report = env.report("plan", "edit.jsonnet", "-V", "owner=platform")
    assert_report(report, [])
