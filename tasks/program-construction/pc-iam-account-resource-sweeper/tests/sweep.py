"""Shared helpers: isolated emulator accounts, configuration files, runs and observations."""

import json
import random
import re
import time
from collections import Counter

import boto3
import yaml
from botocore.exceptions import ClientError

from harness import run_program, workspace, write_fixture

ENDPOINT = "http://engines:4566"
SERVICES = ["sts", "iam", "sqs", "sns", "ssm", "secretsmanager", "kms", "cloudwatchlogs",
            "cloudwatch", "cloudformation", "scheduler", ""]
ALL_TYPES = ["SQSQueue", "SNSTopic", "SNSSubscription", "SSMParameter", "SecretsManagerSecret", "KMSKey",
             "KMSAlias", "CloudWatchLogsLogGroup", "CloudWatchAlarm", "CloudFormationStack", "SchedulerSchedule"]
BLOCKLIST = ["999999999999"]
FAST = ["--no-prompt", "--prompt-delay", "3", "--run-sleep-delay", "1s", "--log-disable-colors"]
SCHEDULE_ROLE = "arn:aws:iam::123456789012:role/scheduler"

# One report line: "<region> - <Type> - <identifier> - [<properties>] - <status>". Whitespace around
# the separators and any leading text (a timestamp, for instance) are not significant.
REPORT = re.compile(r"(?<![A-Za-z0-9-])us-east-1\s+-\s+([A-Za-z]+)\s+-\s+(.+?)\s+-\s+\[(.*)\]\s+-\s+(would remove|filtered\b.*?)\s*$")


class Account:
    """A fresh emulator account: a random 12-digit access key is its own account."""

    def __init__(self):
        self.id = str(random.SystemRandom().randint(10**11, 10**12 - 1))
        self.session = boto3.Session(aws_access_key_id=self.id, aws_secret_access_key="test", region_name="us-east-1")
        self._clients = {}

    def client(self, service):
        if service not in self._clients:
            self._clients[service] = self.session.client(service, endpoint_url=ENDPOINT)
        return self._clients[service]

    def set_alias(self, alias):
        self.client("iam").create_account_alias(AccountAlias=alias)

    # ---- fixtures -------------------------------------------------------------------------
    def queue(self, name, tags=None):
        return self.client("sqs").create_queue(QueueName=name, **({"tags": tags} if tags else {}))["QueueUrl"]

    def queue_arn(self, url):
        return self.client("sqs").get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]

    def topic(self, name, tags=None):
        extra = {"Tags": [{"Key": k, "Value": v} for k, v in tags.items()]} if tags else {}
        return self.client("sns").create_topic(Name=name, **extra)["TopicArn"]

    def subscription(self, topic_arn, queue_url):
        return self.client("sns").subscribe(TopicArn=topic_arn, Protocol="sqs", Endpoint=self.queue_arn(queue_url),
                                            ReturnSubscriptionArn=True)["SubscriptionArn"]

    def parameter(self, name, tags=None, kind="String"):
        extra = {"Tags": [{"Key": k, "Value": v} for k, v in tags.items()]} if tags else {}
        self.client("ssm").put_parameter(Name=name, Value="value", Type=kind, **extra)
        return name

    def secret(self, name, tags=None):
        extra = {"Tags": [{"Key": k, "Value": v} for k, v in tags.items()]} if tags else {}
        return self.client("secretsmanager").create_secret(Name=name, SecretString="s3cret", **extra)["ARN"]

    def key(self, tags=None, alias=None):
        extra = {"Tags": [{"TagKey": k, "TagValue": v} for k, v in tags.items()]} if tags else {}
        key_id = self.client("kms").create_key(Description="fixture", **extra)["KeyMetadata"]["KeyId"]
        if alias:
            self.client("kms").create_alias(AliasName=alias, TargetKeyId=key_id)
        return key_id

    def grant(self, key_id, principal):
        self.client("kms").create_grant(KeyId=key_id, GranteePrincipal=principal, Operations=["Decrypt"])

    def log_group(self, name, tags=None):
        self.client("logs").create_log_group(logGroupName=name, **({"tags": tags} if tags else {}))
        return name

    def alarm(self, name, tags=None):
        extra = {"Tags": [{"Key": k, "Value": v} for k, v in tags.items()]} if tags else {}
        self.client("cloudwatch").put_metric_alarm(
            AlarmName=name, MetricName="Errors", Namespace="Fixture", Statistic="Sum", Period=60,
            EvaluationPeriods=1, Threshold=1, ComparisonOperator="GreaterThanThreshold", **extra)
        return name

    def stack(self, name, resources, tags=None, protected=False):
        extra = {"Tags": [{"Key": k, "Value": v} for k, v in tags.items()]} if tags else {}
        cfn = self.client("cloudformation")
        cfn.create_stack(StackName=name, TemplateBody=json.dumps({"Resources": resources}),
                         EnableTerminationProtection=protected, **extra)
        for _ in range(60):
            status = cfn.describe_stacks(StackName=name)["Stacks"][0]["StackStatus"]
            if not status.endswith("IN_PROGRESS"):
                break
            time.sleep(0.5)
        assert status == "CREATE_COMPLETE", status
        return name

    def schedule(self, name, target_queue_url, group=None):
        scheduler = self.client("scheduler")
        if group and group not in {g["Name"] for g in scheduler.list_schedule_groups()["ScheduleGroups"]}:
            scheduler.create_schedule_group(Name=group)
        scheduler.create_schedule(
            Name=name, ScheduleExpression="rate(5 minutes)", FlexibleTimeWindow={"Mode": "OFF"},
            Target={"Arn": self.queue_arn(target_queue_url), "RoleArn": SCHEDULE_ROLE},
            **({"GroupName": group} if group else {}))
        return name

    # ---- observation ----------------------------------------------------------------------
    def aws_managed(self, *types):
        """Report entries for the emulator's pre-seeded AWS-managed KMS keys and/or aliases."""
        types = types or ("KMSKey", "KMSAlias")
        kms = self.client("kms")
        entries = Counter()
        if "KMSKey" in types:
            for key in kms.list_keys()["Keys"]:
                if kms.describe_key(KeyId=key["KeyId"])["KeyMetadata"].get("KeyManager") == "AWS":
                    entries[("KMSKey", key["KeyId"], "filtered")] += 1
        if "KMSAlias" in types:
            for alias in kms.list_aliases()["Aliases"]:
                if alias["AliasName"].startswith("alias/aws/"):
                    entries[("KMSAlias", alias["AliasName"], "filtered")] += 1
        return entries

    def snapshot(self):
        """Every in-scope resource the account holds (AWS-managed KMS items excluded)."""
        kms = self.client("kms")
        keys = {}
        for key in kms.list_keys()["Keys"]:
            meta = kms.describe_key(KeyId=key["KeyId"])["KeyMetadata"]
            if meta.get("KeyManager") != "AWS":
                keys[key["KeyId"]] = meta["KeyState"]
        live = [s for s in self.client("cloudformation").list_stacks()["StackSummaries"]
                if s["StackStatus"] != "DELETE_COMPLETE"]
        scheduler = self.client("scheduler")
        return {
            "queues": sorted(self.client("sqs").list_queues().get("QueueUrls", [])),
            "topics": sorted(t["TopicArn"] for t in self.client("sns").list_topics()["Topics"]),
            "subscriptions": sorted(s["SubscriptionArn"] for s in self.client("sns").list_subscriptions()["Subscriptions"]),
            "parameters": sorted(p["Name"] for p in self.client("ssm").describe_parameters()["Parameters"]),
            "secrets": sorted(s["Name"] for s in self.client("secretsmanager").list_secrets()["SecretList"]),
            "keys": keys,
            "aliases": sorted(a["AliasName"] for a in kms.list_aliases()["Aliases"] if not a["AliasName"].startswith("alias/aws/")),
            "log_groups": sorted(g["logGroupName"] for g in self.client("logs").describe_log_groups()["logGroups"]),
            "alarms": sorted(a["AlarmName"] for a in self.client("cloudwatch").describe_alarms()["MetricAlarms"]),
            "stacks": sorted(s["StackName"] for s in live),
            "schedules": sorted((s["GroupName"], s["Name"]) for s in scheduler.list_schedules()["Schedules"]),
            # Out of scope, so never removed: schedule groups and the IAM account alias.
            "schedule_groups": sorted(g["Name"] for g in scheduler.list_schedule_groups()["ScheduleGroups"]),
            "account_aliases": sorted(self.client("iam").list_account_aliases().get("AccountAliases", [])),
        }

    # ---- teardown -------------------------------------------------------------------------
    def cleanup(self):
        def quietly(call, *args, **kwargs):
            try:
                call(*args, **kwargs)
            except Exception:  # noqa: BLE001 -- best-effort teardown of a private account
                pass

        try:
            cfn = self.client("cloudformation")
            for s in cfn.list_stacks()["StackSummaries"]:
                if s["StackStatus"] != "DELETE_COMPLETE":
                    quietly(cfn.update_termination_protection, EnableTerminationProtection=False, StackName=s["StackName"])
                    quietly(cfn.delete_stack, StackName=s["StackName"])
            snap = self.snapshot()
        except Exception:  # noqa: BLE001
            return
        sqs, sns, ssm, kms = (self.client(s) for s in ("sqs", "sns", "ssm", "kms"))
        for url in snap["queues"]:
            quietly(sqs.delete_queue, QueueUrl=url)
        for arn in snap["subscriptions"]:
            quietly(sns.unsubscribe, SubscriptionArn=arn)
        for arn in snap["topics"]:
            quietly(sns.delete_topic, TopicArn=arn)
        for name in snap["parameters"]:
            quietly(ssm.delete_parameter, Name=name)
        for name in snap["secrets"]:
            quietly(self.client("secretsmanager").delete_secret, SecretId=name, ForceDeleteWithoutRecovery=True)
        for name in snap["aliases"]:
            quietly(kms.delete_alias, AliasName=name)
        for key_id, state in snap["keys"].items():
            if state != "PendingDeletion":
                quietly(kms.schedule_key_deletion, KeyId=key_id, PendingWindowInDays=7)
        for name in snap["log_groups"]:
            quietly(self.client("logs").delete_log_group, logGroupName=name)
        if snap["alarms"]:
            quietly(self.client("cloudwatch").delete_alarms, AlarmNames=snap["alarms"])
        for group, name in snap["schedules"]:
            quietly(self.client("scheduler").delete_schedule, Name=name, GroupName=group)
        for group in snap["schedule_groups"]:
            if group != "default":
                quietly(self.client("scheduler").delete_schedule_group, Name=group)
        try:
            for alias in self.client("iam").list_account_aliases().get("AccountAliases", []):
                quietly(self.client("iam").delete_account_alias, AccountAlias=alias)
        except ClientError:
            pass


def endpoints():
    return [{"region": "us-east-1", "services": [{"service": s, "url": ENDPOINT} for s in SERVICES]}]


def config(account, includes=None, filters=None, *, account_settings=None, **top):
    """A configuration document for `account`; `top` adds or replaces top-level keys."""
    entry = dict(account_settings or {})
    if filters is not None:
        entry["filters"] = filters
    document = {"regions": ["us-east-1"], "blocklist": list(BLOCKLIST)}
    if includes is not None:
        document["resource-types"] = {"includes": list(includes)}
    document["accounts"] = {account.id: entry}
    document.update(top)
    document["endpoints"] = endpoints()
    return document


class Run:
    def __init__(self, completed):
        self.completed = completed
        self.returncode = completed.returncode
        self.stdout = completed.stdout
        self.stderr = completed.stderr

    def report(self):
        """Counter of (type, identifier, "would remove" | "filtered") from the scan report."""
        entries = Counter()
        for line in self.stdout.splitlines():
            match = REPORT.search(line)
            if match:
                category = "would remove" if match.group(4) == "would remove" else "filtered"
                entries[(match.group(1), match.group(2), category)] += 1
        return entries

    def __repr__(self):
        return f"exit={self.returncode}\nstdout:\n{self.stdout[-6000:]}\nstderr:\n{self.stderr[-3000:]}"


def sweep(account, document, *flags, timeout=55):
    """Run the program as the candidate against `document` (a dict or YAML text)."""
    text = document if isinstance(document, str) else yaml.safe_dump(document, sort_keys=False)
    with workspace() as root:
        path = root / "config.yaml"
        write_fixture(path, text, mode=0o644)
        completed = run_program(["run", "--config", str(path), "--default-region", "us-east-1", *FAST, *flags],
                                cwd=root, timeout=timeout,
                                env={"AWS_ACCESS_KEY_ID": account.id, "AWS_SECRET_ACCESS_KEY": "test"})
    return Run(completed)


def removes(type_, *identifiers):
    return Counter({(type_, i, "would remove"): 1 for i in identifiers})


def keeps(type_, *identifiers):
    return Counter({(type_, i, "filtered"): 1 for i in identifiers})


def expect(*parts):
    total = Counter()
    for part in parts:
        total.update(part)
    return total
