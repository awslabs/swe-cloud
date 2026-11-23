"""Single state-machine operations: create, update, tag reconciliation and convergence."""

import json
from collections import Counter

import pytest
from conftest import (
    LOGGING_OFF,
    ROLE,
    assert_json,
    assert_report,
    assert_stdout,
    changes,
    diff_lines,
    schedule,
    state,
    wait_definition,
)

OTHER_ROLE = "arn:aws:iam::123456789012:role/sfn-exec-v2"


def test_apply_creates_state_machine_with_all_fields(env):
    machine = env.name("full")
    log_group = f"arn:aws:logs:us-east-1:{env.account}:log-group:full:*"
    logging = {"level": "ALL", "includeExecutionData": True, "destinations": [{"cloudWatchLogsLogGroup": {"logGroupArn": log_group}}]}
    definition = {"StartAt": "Pause", "States": {"Pause": {"Type": "Wait", "Seconds": 7, "Next": "Done"}, "Done": {"Type": "Pass", "Result": {"ok": True, "ratio": 0.5, "list": [2, 1]}, "End": True}}}
    env.write("c.jsonnet", [{"state": state(machine, definition=definition, type="EXPRESS", logging=logging, tracing={"enabled": True}, tags=[("Team", "batch"), ("Env", "test")])}])

    proc, report = env.report("apply", "c.jsonnet", "--auto-approve")
    assert_report(report, [(machine, ["create_state"])])
    assert_stdout(proc, report, unchanged_entries=0)

    found = env.machine(machine)
    assert_json(found["definition"], definition)
    assert found["roleArn"] == ROLE and found["type"] == "EXPRESS"
    assert_json(found["logging"], logging)
    assert found["tracing"] is True
    assert found["tags"] == Counter({("Team", "batch"): 1, ("Env", "test"): 1})

    proc, report = env.report("plan", "c.jsonnet")
    assert_report(report, [])
    assert_stdout(proc, report, unchanged_entries=1)


def test_unspecified_logging_and_tracing_never_converge(env):
    machine = env.name("nolog")
    env.write("c.jsonnet", [{"state": state(machine, logging=None, tracing=None)}])
    env.report("apply", "c.jsonnet", "-a")
    created = env.machine(machine)
    assert created is not None
    assert created["logging"]["level"] == "OFF" and created["tracing"] is False

    for command in ("plan", "apply"):
        proc, report = env.report(command, "c.jsonnet", "-a") if command == "apply" else env.report(command, "c.jsonnet")
        assert_report(report, [(machine, ["update_state"])])
        assert_stdout(proc, report, unchanged_entries=0)
        headers, removed, added = diff_lines(report["text_diff"][0])
        assert headers == [(machine, machine)]
        assert (removed, added) == changes(
            ("-", '  "loggingConfiguration": {'),
            ("-", '    "level": "OFF",'),
            ("-", '    "includeExecutionData": false,'),
            ("-", '    "destinations": []'),
            ("-", "  },"),
            ("-", '  "tracingConfiguration": {'),
            ("-", '    "enabled": false'),
            ("-", "  },"),
            ("+", '  "loggingConfiguration": null,'),
            ("+", '  "tracingConfiguration": null,'),
        )
    assert env.machine(machine) == created


def test_json_equal_definition_and_reordered_tags_are_no_change(env):
    machine = env.name("ext")
    pretty = '{\n  "States": {\n    "Pause": {"Seconds": 2, "End": true, "Type": "Wait"}\n  },\n  "StartAt": "Pause",\n  "Comment": "wait job"\n}'
    env.sfn.create_state_machine(name=machine, definition=pretty, roleArn=ROLE, type="STANDARD",
                                 tags=[{"key": "z", "value": "1"}, {"key": "a", "value": "2"}, {"key": "m", "value": "3"}])
    before = env.describe(machine)
    env.write("c.jsonnet", [{"state": state(machine, definition=wait_definition(2), tags=[("a", "2"), ("m", "3"), ("z", "1")])}])
    for command in (["plan"], ["apply", "-a"]):
        proc, report = env.report(command[0], "c.jsonnet", *command[1:])
        assert_report(report, [])
        assert_stdout(proc, report, unchanged_entries=1)
    after = env.describe(machine)
    assert after["definition"] == before["definition"] and after["revisionId"] == before["revisionId"]


def test_update_definition_and_role_leaves_tags_and_schedule(env):
    group = env.group()
    machine = env.name("upd")
    sched = env.schedule_name("upd-sch", group)
    base = {"state": state(machine, definition=wait_definition(2), tags=[("Team", "a")]), "schedule": schedule(env, group, sched, machine)}
    env.write("v1.jsonnet", [base])
    env.report("apply", "v1.jsonnet", "-a")
    schedule_before = env.schedule(group, sched)

    changed = {**base, "state": state(machine, definition=wait_definition(3), role=OTHER_ROLE, tags=[("Team", "a")])}
    env.write("v2.jsonnet", [changed])
    proc, report = env.report("plan", "v2.jsonnet")
    assert_report(report, [(machine, ["update_state"])])
    assert_stdout(proc, report, unchanged_entries=0)
    headers, removed, added = diff_lines(report["text_diff"][0])
    assert headers == [(machine, machine)]
    assert (removed, added) == changes(
        ("-", '        "Seconds": 2,'),
        ("+", '        "Seconds": 3,'),
        ("-", f'  "roleArn": "{ROLE}",'),
        ("+", f'  "roleArn": "{OTHER_ROLE}",'),
    )
    assert env.machine(machine)["definition"] == wait_definition(2)

    env.report("apply", "v2.jsonnet", "-a")
    found = env.machine(machine)
    assert_json(found["definition"], wait_definition(3))
    assert found["roleArn"] == OTHER_ROLE and found["type"] == "STANDARD"
    assert found["tags"] == Counter({("Team", "a"): 1})
    assert env.schedule(group, sched) == schedule_before

    _, report = env.report("plan", "v2.jsonnet")
    assert_report(report, [])


BASE_TAGS = [("keep", "k"), ("change", "old"), ("drop", "d")]


@pytest.mark.parametrize(
    "local, lines",
    [
        (
            [("change", "new"), ("keep", "k"), ("add", "a")],
            [("-", '      "value": "old"'), ("+", '      "value": "new"'), ("-", '      "key": "drop",'), ("-", '      "value": "d"'), ("+", '      "key": "add",'), ("+", '      "value": "a"')],
        ),
        ([("keep", "k"), ("change", "new2"), ("drop", "d")], [("-", '      "value": "old"'), ("+", '      "value": "new2"')]),
        ([("keep", "k"), ("change", "old"), ("drop", "d"), ("add", "a")], [("+", "    },"), ("+", "    {"), ("+", '      "key": "add",'), ("+", '      "value": "a"')]),
        (
            [("keep", "k")],
            [("-", "    },"), ("-", "    {"), ("-", '      "key": "change",'), ("-", '      "value": "old"'), ("-", "    },"), ("-", "    {"), ("-", '      "key": "drop",'), ("-", '      "value": "d"')],
        ),
        (
            [],
            [("-", '  "tags": ['), ("-", "    {"), ("-", "    {"), ("-", "    {"), ("-", "    },"), ("-", "    },"), ("-", "    }"), ("-", "  ]"),
             ("-", '      "key": "keep",'), ("-", '      "value": "k"'), ("-", '      "key": "change",'), ("-", '      "value": "old"'),
             ("-", '      "key": "drop",'), ("-", '      "value": "d"'), ("+", '  "tags": []')],
        ),
    ],
    ids=["change-add-remove", "value-only", "add-only", "remove-several", "remove-all"],
)
def test_tag_reconciliation(env, local, lines):
    machine = env.name("tags")
    env.write("v1.jsonnet", [{"state": state(machine, tags=BASE_TAGS)}])
    env.report("apply", "v1.jsonnet", "-a")
    before = env.machine(machine)

    env.write("v2.jsonnet", [{"state": state(machine, tags=local)}])
    proc, report = env.report("plan", "v2.jsonnet")
    assert_report(report, [(machine, ["update_state"])])
    assert_stdout(proc, report, unchanged_entries=0)
    headers, removed, added = diff_lines(report["text_diff"][0])
    assert headers == [(machine, machine)]
    assert (removed, added) == changes(*lines)
    assert env.machine(machine) == before

    proc, report = env.report("apply", "v2.jsonnet", "-a")
    assert_report(report, [(machine, ["update_state"])])
    after = env.machine(machine)
    assert after["tags"] == Counter(local)
    assert {k: v for k, v in after.items() if k != "tags"} == {k: v for k, v in before.items() if k != "tags"}

    _, report = env.report("plan", "v2.jsonnet")
    assert_report(report, [])


def test_tag_order_alone_is_no_change(env):
    machine = env.name("order")
    env.write("v1.jsonnet", [{"state": state(machine, tags=BASE_TAGS)}])
    env.report("apply", "v1.jsonnet", "-a")
    env.write("v2.jsonnet", [{"state": state(machine, tags=list(reversed(BASE_TAGS)))}])
    proc, report = env.report("plan", "v2.jsonnet")
    assert_report(report, [])
    assert_stdout(proc, report, unchanged_entries=1)


def test_type_change_is_reported_but_not_applied(env):
    machine = env.name("type")
    env.write("v1.jsonnet", [{"state": state(machine, type="STANDARD")}])
    env.report("apply", "v1.jsonnet", "-a")
    env.write("v2.jsonnet", [{"state": state(machine, type="EXPRESS")}])
    for _ in range(2):
        proc, report = env.report("apply", "v2.jsonnet", "-a")
        assert_report(report, [(machine, ["update_state"])])
        _, removed, added = diff_lines(report["text_diff"][0])
        assert (removed, added) == changes(("-", '  "type": "STANDARD",'), ("+", '  "type": "EXPRESS",'))
        assert env.machine(machine)["type"] == "STANDARD"


def test_enable_logging_and_tracing_on_existing_machine(env):
    machine = env.name("logs")
    env.write("v1.jsonnet", [{"state": state(machine)}])
    env.report("apply", "v1.jsonnet", "-a")
    log_group = f"arn:aws:logs:us-east-1:{env.account}:log-group:logs:*"
    logging = {"level": "ERROR", "includeExecutionData": True, "destinations": [{"cloudWatchLogsLogGroup": {"logGroupArn": log_group}}]}
    env.write("v2.jsonnet", [{"state": state(machine, logging=logging, tracing={"enabled": True})}])
    proc, report = env.report("plan", "v2.jsonnet")
    assert_report(report, [(machine, ["update_state"])])
    _, removed, added = diff_lines(report["text_diff"][0])
    assert removed == Counter(['-    "level": "OFF",', '-    "includeExecutionData": false,', '-    "destinations": []', '-    "enabled": false'])
    assert added['+    "level": "ERROR",'] == 1 and added['+    "enabled": true'] == 1
    assert added[f'+          "logGroupArn": "{log_group}"'] == 1

    env.report("apply", "v2.jsonnet", "-a")
    found = env.machine(machine)
    assert_json(found["logging"], logging)
    assert found["tracing"] is True
    _, report = env.report("plan", "v2.jsonnet")
    assert_report(report, [])

    env.write("v3.jsonnet", [{"state": state(machine, logging=LOGGING_OFF)}])
    env.report("apply", "v3.jsonnet", "-a")
    found = env.machine(machine)
    assert found["logging"]["level"] == "OFF" and found["logging"]["destinations"] == []
    assert found["tracing"] is False
    _, report = env.report("plan", "v3.jsonnet")
    assert_report(report, [])
    assert json.loads(env.describe(machine)["definition"]) == state(machine)["definition"]
