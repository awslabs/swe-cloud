"""Shared fixtures: per-test AWS resources with cleanup, config writing and output parsing."""

import json
import re
import uuid
from collections import Counter

import boto3
import pytest
from harness import run_program, workspace, write_fixture

ROLE = "arn:aws:iam::123456789012:role/sfn-exec"
SCHEDULER_ROLE = "arn:aws:iam::123456789012:role/scheduler-invoke"
OPS = ("create_state", "update_state", "create_schedule", "update_schedule", "delete_schedule", "delete_state")
LOGGING_OFF = {"level": "OFF", "includeExecutionData": False, "destinations": []}
TRACING_OFF = {"enabled": False}

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
LOG_LINE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d")


def pass_definition(result=1):
    return {"StartAt": "Prepare", "States": {"Prepare": {"Type": "Pass", "Result": {"n": result}, "Next": "Done"}, "Done": {"Type": "Succeed"}}}


def wait_definition(seconds):
    return {"Comment": "wait job", "StartAt": "Pause", "States": {"Pause": {"Type": "Wait", "Seconds": seconds, "End": True}}}


def same_json(actual, expected):
    """JSON equality that keeps booleans apart from numbers and compares numbers by value."""
    if isinstance(expected, bool):
        return type(actual) is bool and actual == expected
    if isinstance(expected, (int, float)):
        return isinstance(actual, (int, float)) and not isinstance(actual, bool) and actual == expected
    if expected is None or isinstance(expected, str):
        return type(actual) is type(expected) and actual == expected
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(map(same_json, actual, expected))
    if isinstance(expected, dict):
        return isinstance(actual, dict) and actual.keys() == expected.keys() and all(same_json(actual[k], expected[k]) for k in expected)
    raise TypeError(expected)


def assert_json(actual, expected):
    assert same_json(actual, expected), f"\nactual:   {json.dumps(actual, sort_keys=True)}\nexpected: {json.dumps(expected, sort_keys=True)}"


def tag_counter(tags):
    assert isinstance(tags, list)
    for tag in tags:
        assert isinstance(tag, dict) and set(tag) == {"key", "value"}, tag
    return Counter((tag["key"], tag["value"]) for tag in tags)


def output_lines(stdout):
    """stdout without colour escapes and without diagnostic log lines."""
    return [line for line in ANSI.sub("", stdout).split("\n") if not LOG_LINE.match(line)]


def diff_lines(text):
    """(header pairs in order, Counter of removed lines, Counter of added lines) of one text diff entry."""
    headers, removed, added = [], Counter(), Counter()
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if line.startswith("--- remote: "):
            following = lines[index + 1] if index + 1 < len(lines) else ""
            assert following.startswith("+++ local:  "), text
            pair = (line[len("--- remote: "):], following[len("+++ local:  "):])
            if not headers or headers[-1] != pair:
                headers.append(pair)
        elif line.startswith("+++ local:  "):
            continue
        elif line.startswith("-"):
            removed[line] += 1
        elif line.startswith("+"):
            added[line] += 1
    return headers, removed, added


def added_block(text):
    """Literal multi-line rendering -> Counter of '+' lines."""
    return Counter("+" + line for line in text.strip("\n").split("\n"))


def changes(*pairs):
    """[('-', line), ('+', line), ...] -> (removed Counter, added Counter)."""
    removed, added = Counter(), Counter()
    for sign, line in pairs:
        (removed if sign == "-" else added)[sign + line] += 1
    return removed, added


class Env:
    def __init__(self, root, account):
        self.root = root
        self.account = account
        self.suffix = uuid.uuid4().hex[:10]
        self.sfn = boto3.client("stepfunctions")
        self.scheduler = boto3.client("scheduler")
        self.machines, self.schedules, self.groups = [], [], []

    # ---- names and resources owned by this test
    def name(self, label):
        name = f"{label}-{self.suffix}"
        self.machines.append(name)
        return name

    def schedule_name(self, label, group):
        name = f"{label}-{self.suffix}"
        self.schedules.append((group, name))
        return name

    def group(self, label="grp"):
        name = f"{label}-{self.suffix}"
        self.scheduler.create_schedule_group(Name=name)
        self.groups.append(name)
        return name

    def arn(self, name):
        return f"arn:aws:states:us-east-1:{self.account}:stateMachine:{name}"

    # ---- files
    def write(self, filename, content):
        if not isinstance(content, str):
            content = json.dumps(content, indent=1)
        path = self.root / filename
        write_fixture(path, content)
        return path

    def read_json(self, filename):
        return json.loads((self.root / filename).read_text())

    # ---- program
    def run(self, *args, stdin=None):
        return run_program(list(args), cwd=self.root, stdin=stdin)

    def report(self, command, config, *args, stdin=None, fails=False):
        """Run plan/apply with --diff-as-json and return (process, parsed report); fails=True expects a non-zero exit."""
        out = f"report-{uuid.uuid4().hex[:6]}.json"
        proc = self.run(command, "-c", config, *args, "-o", out, stdin=stdin)
        assert (proc.returncode != 0) is fails, f"exit {proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        return proc, self.read_json(out)

    # ---- remote observation
    def describe(self, name):
        try:
            found = self.sfn.describe_state_machine(stateMachineArn=self.arn(name))
        except self.sfn.exceptions.StateMachineDoesNotExist:
            return None
        if found.get("status") == "DELETING":
            return None
        return found

    def machine(self, name):
        """Graded view of a state machine, or None."""
        found = self.describe(name)
        if found is None:
            return None
        logging = found.get("loggingConfiguration") or {}
        return {
            "definition": json.loads(found["definition"]),
            "roleArn": found["roleArn"],
            "type": found["type"],
            "logging": {"level": logging.get("level", "OFF"), "includeExecutionData": logging.get("includeExecutionData", False), "destinations": logging.get("destinations", [])},
            "tracing": (found.get("tracingConfiguration") or {}).get("enabled", False),
            "tags": tag_counter(self.sfn.list_tags_for_resource(resourceArn=self.arn(name))["tags"]),
        }

    def schedule(self, group, name):
        try:
            found = self.scheduler.get_schedule(GroupName=group, Name=name)
        except self.scheduler.exceptions.ResourceNotFoundException:
            return None
        for volatile in ("ResponseMetadata", "CreationDate", "LastModificationDate"):
            found.pop(volatile, None)
        return found

    def assert_schedule(self, group, name, expected):
        """The complete GetSchedule view (minus volatile fields and the Arn) equals expected."""
        found = self.schedule(group, name)
        assert found is not None
        assert found.pop("Arn").endswith(f"schedule/{group}/{name}")
        found.pop("ActionAfterCompletion", None)
        assert found == expected, f"\nactual:   {found}\nexpected: {expected}"

    def cleanup(self):
        for group, name in self.schedules:
            try:
                self.scheduler.delete_schedule(GroupName=group, Name=name)
            except Exception:
                pass
        for name in self.machines:
            try:
                self.sfn.delete_state_machine(stateMachineArn=self.arn(name))
            except Exception:
                pass
        for group in self.groups:
            try:
                self.scheduler.delete_schedule_group(Name=group)
            except Exception:
                pass


@pytest.fixture(scope="session")
def account():
    return boto3.client("sts").get_caller_identity()["Account"]


@pytest.fixture
def env(account):
    with workspace() as root:
        environment = Env(root, account)
        try:
            yield environment
        finally:
            environment.cleanup()


def state(name, *, definition=None, role=ROLE, type="STANDARD", tags=(), logging=LOGGING_OFF, tracing=TRACING_OFF):
    entry = {"name": name, "type": type, "roleArn": role, "definition": definition or pass_definition(), "tags": [{"key": k, "value": v} for k, v in tags]}
    if logging is not None:
        entry["loggingConfiguration"] = logging
    if tracing is not None:
        entry["tracingConfiguration"] = tracing
    return entry


def schedule(env, group, name, machine, **fields):
    entry = {
        "groupName": group,
        "name": name,
        "state": "ENABLED",
        "scheduleExpression": "rate(1 hour)",
        "flexibleTimeWindow": {"mode": "OFF"},
        "target": {"arn": env.arn(machine), "roleArn": SCHEDULER_ROLE, "input": "{}"},
    }
    entry.update(fields)
    return entry


def assert_report(report, expected_ops):
    """expected_ops: list of (state name, [ops]) in config order for the entries that change."""
    assert set(report) == {"text_diff", "diff_ops", "no_change", "summary"}
    assert isinstance(report["diff_ops"], list)
    assert [(item.get("state_name"), item.get("diff_ops")) for item in report["diff_ops"]] == [(n, list(o)) for n, o in expected_ops]
    for item in report["diff_ops"]:
        assert set(item) == {"state_name", "diff_ops"}
    assert type(report["no_change"]) is bool and report["no_change"] is (not expected_ops)
    counts = Counter(op for _, ops in expected_ops for op in ops)
    assert_json(report["summary"], {op: counts[op] for op in OPS})
    assert isinstance(report["text_diff"], list) and len(report["text_diff"]) == len(expected_ops)
    assert all(isinstance(text, str) and "\x1b" not in text for text in report["text_diff"])


def assert_stdout(proc, report, unchanged_entries):
    """stdout carries every text diff, one 'no difference' per unchanged entry, and the final block."""
    lines = output_lines(proc.stdout)
    text = "\n".join(lines)
    position = 0
    for entry in report["text_diff"]:
        found = text.find(entry.rstrip("\n"), position)
        assert found >= 0, f"text diff missing from stdout (or out of config order):\n{entry}\n---- stdout:\n{text}"
        position = found + len(entry.rstrip("\n"))
    assert lines.count("no difference") == unchanged_entries, text
    if report["no_change"]:
        assert "No diff found. Fubura will do nothing." in lines, text
        assert "Fubura will:" not in lines
    else:
        assert "No diff found. Fubura will do nothing." not in lines
        index = lines.index("Fubura will:")
        assert Counter(lines[index + 1:index + 7]) == Counter(f"    {op}: {n}" for op, n in report["summary"].items()), text
