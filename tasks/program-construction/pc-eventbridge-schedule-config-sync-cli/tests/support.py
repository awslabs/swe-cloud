"""Task-local helpers: candidate invocation, fixture files, AWS snapshots and comparisons."""

import json
import re
from datetime import timezone

import boto3
import yaml
from harness import run_program, write_fixture

TRACKING_KEY = "ebschedule-tracking-id"


# --- invocation and fixtures -------------------------------------------------


def cli(*args, cwd, env=None, stdin=None):
    return run_program(list(args), cwd=cwd, env=env, stdin=stdin)


def write(root, relative, text):
    path = root / relative
    write_fixture(path, text, mode=0o644)
    return path


def lines(text):
    return text.splitlines()


# --- YAML reading ------------------------------------------------------------


class Yaml12Loader(yaml.SafeLoader):
    """SafeLoader with YAML 1.2 core scalars: only true/false are booleans, no timestamps,
    decimal/0o/0x integers and exponent floats. Unquoted OFF/on/yes and RFC 3339 dates
    stay strings, so quoting style is not significant."""


_REPLACED = ("tag:yaml.org,2002:bool", "tag:yaml.org,2002:timestamp", "tag:yaml.org,2002:int", "tag:yaml.org,2002:float")
Yaml12Loader.yaml_implicit_resolvers = {
    first: [(tag, regexp) for tag, regexp in resolvers if tag not in _REPLACED]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
Yaml12Loader.add_implicit_resolver("tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF"))
Yaml12Loader.add_implicit_resolver("tag:yaml.org,2002:int", re.compile(r"^(?:[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+)$"), list("-+0123456789"))
Yaml12Loader.add_implicit_resolver(
    "tag:yaml.org,2002:float",
    re.compile(r"^(?:[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$"),
    list("-+.0123456789"),
)


def _int(loader, node):
    value = loader.construct_scalar(node)
    return int(value, 0) if value[:2] in ("0o", "0x") else int(value, 10)


def _float(loader, node):
    value = loader.construct_scalar(node).lower()
    return float(value.replace(".inf", "inf").replace(".nan", "nan"))


Yaml12Loader.add_constructor("tag:yaml.org,2002:int", _int)
Yaml12Loader.add_constructor("tag:yaml.org,2002:float", _float)


def load_yaml(text):
    return yaml.load(text, Loader=Yaml12Loader)


# --- comparison --------------------------------------------------------------


def same(actual, expected, path="$"):
    """Structural equality keeping booleans distinct from numbers; numbers compare by value."""
    if isinstance(expected, bool) or isinstance(actual, bool):
        assert type(actual) is bool and type(expected) is bool and actual is expected, f"{path}: {actual!r} != {expected!r}"
    elif isinstance(expected, (int, float)):
        assert type(actual) in (int, float), f"{path}: {actual!r} is not a number (expected {expected!r})"
        assert actual == expected, f"{path}: {actual!r} != {expected!r}"
    elif isinstance(expected, dict):
        assert isinstance(actual, dict), f"{path}: {actual!r} is not a mapping"
        assert set(actual) == set(expected), f"{path}: keys {sorted(actual)} != {sorted(expected)}"
        for key in expected:
            same(actual[key], expected[key], f"{path}.{key}")
    elif isinstance(expected, list):
        assert isinstance(actual, list), f"{path}: {actual!r} is not a sequence"
        assert len(actual) == len(expected), f"{path}: {len(actual)} items != {len(expected)}: {actual!r}"
        for index, (a, e) in enumerate(zip(actual, expected)):
            same(a, e, f"{path}[{index}]")
    else:
        assert type(actual) is type(expected) and actual == expected, f"{path}: {actual!r} != {expected!r}"


def same_json(text, expected):
    same(json.loads(text), expected)


def _diff_line(line):
    """Split a unified-diff line into comparable parts; YAML scalars compare by meaning."""
    if line.startswith(("--- ", "+++ ", "@@ ", "# ")) or not line:
        return ("raw", line)
    marker, content = line[0], line[1:]
    if marker not in " +-":
        return ("raw", line)
    indent = len(content) - len(content.lstrip(" "))
    body = content.strip(" ")
    if not body:
        return (marker, indent, "blank", None)
    try:
        value = load_yaml(body)
    except yaml.YAMLError:
        return (marker, indent, "text", body)
    return (marker, indent, "yaml", value)


def assert_diff(actual, expected):
    """Compare diff output line by line under the public diff observation rule."""
    got, want = actual.splitlines(), expected.splitlines()
    assert len(got) == len(want), f"diff has {len(got)} lines, expected {len(want)}:\n{actual}"
    for number, (a, e) in enumerate(zip(got, want), 1):
        pa, pe = _diff_line(a), _diff_line(e)
        message = f"diff line {number}: {a!r} != {e!r}\n{actual}"
        assert pa[:3] == pe[:3], message
        if pe[0] == "raw":
            assert a == e, message
        elif pe[2] == "yaml":
            try:
                same(pa[3], pe[3])
            except AssertionError as error:
                raise AssertionError(f"{message}\n{error}") from None
        else:
            assert pa[3] == pe[3], message


def changed_lines(diff_text):
    """The added/removed body lines of a diff, as text."""
    return "".join(
        line + "\n" for line in diff_text.splitlines() if line[:1] in "+-" and not line.startswith(("--- ", "+++ "))
    )


def assert_problems(stderr, path, problems, total=None):
    """validate: one problem per line (order not significant) then the summary line."""
    got = stderr.splitlines()
    total = len(problems) if total is None else total
    assert got[-1:] == [f"error: validation failed: {total} problem(s)"], stderr
    want = sorted(f"{path}: {problem}" for problem in problems)
    assert sorted(got[:-1]) == want, stderr


# --- AWS observation ---------------------------------------------------------


def events():
    return boto3.client("events")


def scheduler():
    return boto3.client("scheduler")


def account():
    return boto3.client("sts").get_caller_identity()["Account"]


def rule_snapshot(name, bus="default"):
    """DescribeRule + ListTargetsByRule + ListTagsForResource, or None if absent."""
    client = events()
    try:
        rule = client.describe_rule(Name=name, EventBusName=bus)
    except client.exceptions.ResourceNotFoundException:
        return None
    fields = {key: rule[key] for key in ("Name", "State", "Description", "ScheduleExpression", "RoleArn") if rule.get(key)}
    if rule.get("EventPattern"):
        fields["EventPattern"] = json.loads(rule["EventPattern"])
    tags = {tag["Key"]: tag["Value"] for tag in client.list_tags_for_resource(ResourceARN=rule["Arn"])["Tags"]}
    targets = []
    for target in client.list_targets_by_rule(Rule=name, EventBusName=bus)["Targets"]:
        target = _present(target)
        if "Input" in target:
            target["Input"] = json.loads(target["Input"])
        targets.append(target)
    ids = [target["Id"] for target in targets]
    assert len(ids) == len(set(ids)), targets
    return {"rule": fields, "tags": tags, "targets": {target.pop("Id"): target for target in targets}}


def rule_names(prefix, bus="default"):
    return sorted(rule["Name"] for rule in events().list_rules(NamePrefix=prefix, EventBusName=bus)["Rules"])


SCHEDULER_DEFAULTS = {"ActionAfterCompletion": "NONE", "ScheduleExpressionTimezone": "UTC"}
DEFAULT_RETRY = {"MaximumRetryAttempts": 185, "MaximumEventAgeInSeconds": 86400}


def _present(mapping):
    """Drop absent-equivalent values: None, empty strings and empty collections."""
    return {key: value for key, value in mapping.items() if value not in (None, "", {}, [])}


def schedule_snapshot(group, name):
    """GetSchedule without timestamps/ARN; Scheduler defaults and empty values count as absent."""
    client = scheduler()
    try:
        schedule = client.get_schedule(GroupName=group, Name=name)
    except client.exceptions.ResourceNotFoundException:
        return None
    for key in ("ResponseMetadata", "Arn", "CreationDate", "LastModificationDate"):
        schedule.pop(key, None)
    for key in ("StartDate", "EndDate"):
        if key in schedule:
            schedule[key] = schedule[key].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    target = schedule.get("Target", {})
    if "Input" in target:
        target["Input"] = json.loads(target["Input"])
    schedule = _present({key: value for key, value in schedule.items() if SCHEDULER_DEFAULTS.get(key) != value})
    target = _present(target)
    if target.get("RetryPolicy") == DEFAULT_RETRY:
        del target["RetryPolicy"]
    schedule["Target"] = target
    return schedule


def schedule_names(group, prefix=""):
    found = []
    for page in scheduler().get_paginator("list_schedules").paginate(GroupName=group):
        found.extend(item["Name"] for item in page["Schedules"] if item["Name"].startswith(prefix))
    return sorted(found)


def group_tags(name):
    """Tags of a schedule group, or None if the group does not exist."""
    client = scheduler()
    try:
        arn = client.get_schedule_group(Name=name)["Arn"]
    except client.exceptions.ResourceNotFoundException:
        return None
    return {tag["Key"]: tag["Value"] for tag in client.list_tags_for_resource(ResourceArn=arn)["Tags"]}


def put_rule(name, *, bus="default", expression="rate(1 hour)", state="ENABLED", tags=None, targets=()):
    """Create a rule out of band (not through the program under test)."""
    client = events()
    arn = client.put_rule(Name=name, EventBusName=bus, ScheduleExpression=expression, State=state)["RuleArn"]
    if tags:
        client.tag_resource(ResourceARN=arn, Tags=[{"Key": k, "Value": v} for k, v in tags.items()])
    if targets:
        client.put_targets(Rule=name, EventBusName=bus, Targets=list(targets))
    return arn


def create_group(name, tags=None):
    scheduler().create_schedule_group(Name=name, Tags=[{"Key": k, "Value": v} for k, v in (tags or {}).items()])


def put_schedule(group, name, arn, role, expression="rate(1 hour)"):
    scheduler().create_schedule(
        Name=name,
        GroupName=group,
        ScheduleExpression=expression,
        FlexibleTimeWindow={"Mode": "OFF"},
        Target={"Arn": arn, "RoleArn": role},
    )


def cleanup(prefix):
    """Delete every rule, schedule, group, bus and parameter whose name starts with prefix."""
    eb, sch = events(), scheduler()
    buses = ["default"] + [bus["Name"] for bus in eb.list_event_buses()["EventBuses"] if bus["Name"].startswith(prefix)]
    for bus in buses:
        for rule in eb.list_rules(NamePrefix=prefix, EventBusName=bus)["Rules"]:
            ids = [target["Id"] for target in eb.list_targets_by_rule(Rule=rule["Name"], EventBusName=bus)["Targets"]]
            if ids:
                eb.remove_targets(Rule=rule["Name"], EventBusName=bus, Ids=ids)
            eb.delete_rule(Name=rule["Name"], EventBusName=bus)
    for bus in buses[1:]:
        eb.delete_event_bus(Name=bus)
    groups = [group["Name"] for group in sch.list_schedule_groups()["ScheduleGroups"]]
    for group in groups:
        if group == "default" or group.startswith(prefix):
            for name in schedule_names(group, prefix if group == "default" else ""):
                sch.delete_schedule(GroupName=group, Name=name)
            if group != "default":
                sch.delete_schedule_group(Name=group)
    ssm = boto3.client("ssm")
    names = [p["Name"] for p in ssm.get_parameters_by_path(Path=f"/{prefix}", Recursive=True).get("Parameters", [])]
    for name in names:
        ssm.delete_parameter(Name=name)
