"""Discovery: which resources each type reports, their identifiers, built-in skips and type selection."""

import pytest

from sweep import ALL_TYPES, config, expect, keeps, removes, sweep


def populate(account):
    """One or more resources of every in-scope type; returns the expected dry-run report."""
    target = account.queue("lst-target")
    topic = account.topic("lst-topic", tags={"team": "core"})
    sub = account.subscription(topic, target)
    secret = account.secret("lst-secret")
    managed_secret = account.secret("lst-managed", tags={"aws:secretsmanager:owningService": "rds"})
    key = account.key(alias="alias/lst-key")
    bare_key = account.key(tags={"team": "core"})
    account.stack("lst-stack", {
        "Q": {"Type": "AWS::SQS::Queue", "Properties": {"QueueName": "lst-stack-queue"}},
        "P": {"Type": "AWS::SSM::Parameter", "Properties": {"Name": "/lst/stack", "Type": "String", "Value": "v"}},
    })
    stack_queue = [u for u in account.snapshot()["queues"] if u.endswith("/lst-stack-queue")]
    assert len(stack_queue) == 1
    account.schedule("lst-default", target)
    account.schedule("lst-grouped", target, group="lst-group")
    account.schedule("lst-default", target, group="lst-group")  # same name, second group: reported twice
    return expect(
        removes("SQSQueue", target, stack_queue[0]),
        removes("SNSTopic", f"TopicARN: {topic}"),
        removes("SNSSubscription", f"Owner: {account.id} ARN: {sub}"),
        removes("SSMParameter", account.parameter("/lst/plain"), account.parameter("/lst/secure", kind="SecureString"), "/lst/stack"),
        removes("SecretsManagerSecret", secret),
        keeps("SecretsManagerSecret", managed_secret),
        removes("KMSKey", key, bare_key),
        removes("KMSAlias", "alias/lst-key"),
        removes("CloudWatchLogsLogGroup", account.log_group("/lst/group")),
        removes("CloudWatchAlarm", account.alarm("lst-alarm")),
        removes("CloudFormationStack", "lst-stack"),
        removes("SchedulerSchedule", "lst-default", "lst-grouped"),
        removes("SchedulerSchedule", "lst-default"),
        account.aws_managed(),
    )


def test_every_type_reported(account):
    expected = populate(account)
    before = account.snapshot()
    run = sweep(account, config(account, ALL_TYPES))
    assert run.returncode == 0, run
    assert run.report() == expected, run
    assert account.snapshot() == before


def test_aws_managed_kms_items_only(account):
    run = sweep(account, config(account, ["KMSKey", "KMSAlias"]), "--no-dry-run")
    assert run.returncode == 0, run
    managed = account.aws_managed()
    assert managed, "the emulator account should hold AWS-managed keys"
    assert run.report() == managed, run
    assert all(state == "Enabled" for state in account.snapshot()["keys"].values())
    assert account.aws_managed() == managed


def test_aws_managed_secret_survives_removal(account):
    managed = account.secret("svc-owned", tags={"aws:secretsmanager:owningService": "rds"})
    plain = account.secret("svc-plain")
    run = sweep(account, config(account, ["SecretsManagerSecret"]), "--no-dry-run")
    assert run.returncode == 0, run
    assert run.report() == expect(keeps("SecretsManagerSecret", managed), removes("SecretsManagerSecret", plain)), run
    assert account.snapshot()["secrets"] == ["svc-owned"]


def selection_fixture(account):
    queue = account.queue("sel-queue")
    parameter = account.parameter("/sel/param")
    group = account.log_group("/sel/group")
    key = account.key(alias="alias/sel")
    return {
        "SQSQueue": removes("SQSQueue", queue),
        "SSMParameter": removes("SSMParameter", parameter),
        "CloudWatchLogsLogGroup": removes("CloudWatchLogsLogGroup", group),
        "KMSKey": removes("KMSKey", key),
        "KMSAlias": removes("KMSAlias", "alias/sel"),
    }


# (top-level resource-types, account resource-types, command-line flags, selected types)
SELECTIONS = {
    "top-and-account-includes-intersect": (
        {"includes": ["SQSQueue", "SSMParameter", "CloudWatchLogsLogGroup"]},
        {"includes": ["SQSQueue", "SSMParameter", "KMSKey"]}, [], ["SQSQueue", "SSMParameter"]),
    "cli-exclude": (
        {"includes": ["SQSQueue", "SSMParameter", "CloudWatchLogsLogGroup"]},
        {"includes": ["SQSQueue", "SSMParameter", "KMSKey"]}, ["--exclude", "SSMParameter"], ["SQSQueue"]),
    "cli-include-disjoint": (
        {"includes": ["SQSQueue", "SSMParameter"]}, None, ["--include", "CloudWatchLogsLogGroup"], []),
    "cli-include-repeated": (
        {"includes": ["SQSQueue", "SSMParameter", "CloudWatchLogsLogGroup"]}, None,
        ["--include", "SQSQueue", "--target", "CloudWatchLogsLogGroup"], ["SQSQueue", "CloudWatchLogsLogGroup"]),
    "excludes-top-and-account": (
        {"includes": ["SQSQueue", "SSMParameter", "CloudWatchLogsLogGroup"], "excludes": ["SQSQueue"]},
        {"excludes": ["CloudWatchLogsLogGroup"]}, [], ["SSMParameter"]),
    "account-only-includes": (
        None, {"includes": ["CloudWatchLogsLogGroup", "SSMParameter"]}, [], ["CloudWatchLogsLogGroup", "SSMParameter"]),
    "wildcard-include": ({"includes": ["KMS*"]}, None, [], ["KMSKey", "KMSAlias"]),
    "cli-only-includes": (None, None, ["--include", "SQSQueue", "--include", "KMSAlias"], ["SQSQueue", "KMSAlias"]),
}


@pytest.mark.parametrize("case", sorted(SELECTIONS))
def test_resource_type_selection(account, case):
    top, own, flags, selected = SELECTIONS[case]
    entries = selection_fixture(account)
    document = config(account, account_settings={"resource-types": own} if own else None)
    if top:
        document = {"resource-types": top, **document}
    expected = expect(*(entries[t] for t in selected))
    kms_types = [t for t in selected if t.startswith("KMS")]
    if kms_types:
        expected.update(account.aws_managed(*kms_types))
    before = account.snapshot()
    run = sweep(account, document, *flags)
    assert run.returncode == 0, run
    assert run.report() == expected, run
    assert account.snapshot() == before
