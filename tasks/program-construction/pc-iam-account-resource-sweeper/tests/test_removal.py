"""Real removal: per-type deletion effects, retries until stable, waiting, holds and failures."""

from sweep import ALL_TYPES, config, expect, keeps, removes, sweep

KEEP = {"__global__": [{"property": "tag:keep", "value": "yes"}]}
TAG = {"keep": "yes"}


def test_removes_every_type_and_keeps_filtered(account):
    target = account.queue("rm-target")
    kept_queue = account.queue("rm-kept", tags=TAG)
    topic, kept_topic = account.topic("rm-topic"), account.topic("rm-kept-topic", tags=TAG)
    account.subscription(topic, target)
    account.parameter("/rm/param")
    kept_param = account.parameter("/rm/kept", tags=TAG)
    account.secret("rm-secret")
    account.secret("rm-kept-secret", tags=TAG)
    key = account.key(alias="alias/rm-key")
    kept_key = account.key(tags=TAG, alias="alias/rm-kept-key")
    account.log_group("/rm/group")
    kept_group = account.log_group("/rm/kept", tags=TAG)
    account.alarm("rm-alarm")
    kept_alarm = account.alarm("rm-kept-alarm", tags=TAG)
    account.stack("rm-stack", {
        "Q": {"Type": "AWS::SQS::Queue", "Properties": {"QueueName": "rm-stack-queue"}},
        "P": {"Type": "AWS::SSM::Parameter", "Properties": {"Name": "/rm/stack", "Type": "String", "Value": "v"}},
    })
    kept_stack = account.stack("rm-kept-stack", {
        "P": {"Type": "AWS::SSM::Parameter", "Properties": {"Name": "/rm/kept-stack", "Type": "String", "Value": "v"}},
    }, tags=TAG)
    account.schedule("rm-default", target)
    account.schedule("rm-grouped", target, group="rm-group")
    before = account.snapshot()
    run = sweep(account, config(account, ALL_TYPES, {**KEEP, "SSMParameter": ["/rm/kept-stack"]}), "--no-dry-run")
    assert run.returncode == 0, run
    after = account.snapshot()
    assert after == {
        **before,
        "queues": [kept_queue],
        "topics": [kept_topic],
        "subscriptions": [],
        "parameters": sorted([kept_param, "/rm/kept-stack"]),
        "secrets": ["rm-kept-secret"],
        "keys": {key: "PendingDeletion", kept_key: "Enabled"},
        # Aliases expose their key's tags as key:tag:<k>, so the global tag:keep filter does not keep one.
        "aliases": [],
        "log_groups": [kept_group],
        "alarms": [kept_alarm],
        "stacks": [kept_stack],
        "schedules": [],
    }, run


def test_stack_owned_resources_settle(account):
    account.stack("own-stack", {
        "Q": {"Type": "AWS::SQS::Queue", "Properties": {"QueueName": "own-stack-queue"}},
        "P": {"Type": "AWS::SSM::Parameter", "Properties": {"Name": "/own/stack", "Type": "String", "Value": "v"}},
    })
    free = account.queue("own-free")
    before = account.snapshot()
    stack_queue = [u for u in before["queues"] if u != free]
    run = sweep(account, config(account, ["CloudFormationStack", "SQSQueue", "SSMParameter"]), "--no-dry-run")
    assert run.returncode == 0, run
    assert run.report() == expect(removes("CloudFormationStack", "own-stack"), removes("SQSQueue", free, *stack_queue),
                                  removes("SSMParameter", "/own/stack")), run
    assert account.snapshot() == {**before, "queues": [], "parameters": [], "stacks": []}


def test_kms_keys_pend_and_second_run_skips_them(account):
    deleted = account.key(alias="alias/kms-del")
    kept = account.key(tags=TAG)
    orphan_alias_key = account.key(tags=TAG, alias="alias/kms-orphan")
    document = config(account, ["KMSKey", "KMSAlias"], KEEP)
    run = sweep(account, document, "--no-dry-run")
    assert run.returncode == 0, run
    assert run.report() == expect(removes("KMSKey", deleted), keeps("KMSKey", kept, orphan_alias_key),
                                  removes("KMSAlias", "alias/kms-del", "alias/kms-orphan"), account.aws_managed()), run
    snapshot = account.snapshot()
    assert snapshot["keys"] == {deleted: "PendingDeletion", kept: "Enabled", orphan_alias_key: "Enabled"}
    assert snapshot["aliases"] == []
    run = sweep(account, document, "--no-dry-run")
    assert run.returncode == 0, run
    assert run.report() == expect(keeps("KMSKey", deleted, kept, orphan_alias_key), account.aws_managed()), run
    assert account.snapshot() == snapshot


def test_service_grant_holds_key_until_wait_limit(account):
    held = account.key(alias="alias/held")
    account.grant(held, "rds.amazonaws.com")
    iam_granted = account.key()
    account.grant(iam_granted, f"arn:aws:iam::{account.id}:role/app")
    account.parameter("/held/param")
    before = account.snapshot()
    run = sweep(account, config(account, ["KMSKey", "KMSAlias", "SSMParameter"]), "--no-dry-run", "--max-wait-retries", "2")
    assert run.returncode != 0, run
    assert run.report() == expect(removes("KMSKey", held, iam_granted), removes("KMSAlias", "alias/held"),
                                  removes("SSMParameter", "/held/param"), account.aws_managed()), run
    assert account.snapshot() == {**before, "keys": {held: "Enabled", iam_granted: "PendingDeletion"},
                                  "aliases": [], "parameters": []}


def test_delete_in_use_keys_setting(account):
    held = account.key()
    account.grant(held, "rds.amazonaws.com")
    plain = account.key()
    document = config(account, ["KMSKey"], settings={"KMSKey": {"DeleteInUseKeys": True}})
    run = sweep(account, document, "--no-dry-run", "--max-wait-retries", "2")
    assert run.returncode == 0, run
    assert account.snapshot()["keys"] == {held: "PendingDeletion", plain: "PendingDeletion"}


def test_protected_stack_fails_run(account):
    account.stack("prot-stack", {"P": {"Type": "AWS::SSM::Parameter",
                                       "Properties": {"Name": "/prot/owned", "Type": "String", "Value": "v"}}},
                  protected=True)
    account.parameter("/prot/free")
    account.queue("prot-untouched")
    before = account.snapshot()
    run = sweep(account, config(account, ["CloudFormationStack", "SSMParameter"],
                                {"SSMParameter": ["/prot/owned"]}), "--no-dry-run")
    assert run.returncode != 0, run
    assert run.report() == expect(removes("CloudFormationStack", "prot-stack"), removes("SSMParameter", "/prot/free"),
                                  keeps("SSMParameter", "/prot/owned")), run
    assert account.snapshot() == {**before, "parameters": ["/prot/owned"]}


def test_disable_deletion_protection_setting(account):
    account.stack("unprot-stack", {"P": {"Type": "AWS::SSM::Parameter",
                                         "Properties": {"Name": "/unprot/owned", "Type": "String", "Value": "v"}}},
                  protected=True)
    before = account.snapshot()
    document = config(account, ["CloudFormationStack"],
                      settings={"CloudFormationStack": {"DisableDeletionProtection": True}})
    run = sweep(account, document, "--no-dry-run")
    assert run.returncode == 0, run
    assert run.report() == removes("CloudFormationStack", "unprot-stack"), run
    assert account.snapshot() == {**before, "stacks": [], "parameters": []}


def test_failed_filter_evaluation_changes_nothing(account):
    account.parameter("/bad/when", tags={"created": "soon"})
    account.parameter("/bad/plain")
    account.queue("bad-queue")
    before = account.snapshot()
    for rules in (
        [{"property": "tag:created", "type": "dateOlderThan", "value": "24h"}],
        [{"property": "Name", "type": "dateOlderThanNow", "value": "1h"}],
        [{"property": "Name", "type": "dateOlderThan", "value": "soon"}],
        [{"property": "Name", "type": "regex", "value": "("}],
        [{"property": "Name", "type": "bogus", "value": "x"}],
        [{"property": "", "value": ""}],
    ):
        run = sweep(account, config(account, ["SSMParameter", "SQSQueue"], {"SSMParameter": rules}), "--no-dry-run")
        assert run.returncode != 0, (rules, str(run))
        assert account.snapshot() == before, rules
