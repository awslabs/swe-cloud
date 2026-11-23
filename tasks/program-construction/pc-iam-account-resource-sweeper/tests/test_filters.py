"""The filter language: identifiers, properties, operators, dates, invert, globals, presets and groups."""

import datetime

import pytest

from sweep import config, expect, keeps, removes, sweep


def classify(account, document, *flags):
    before = account.snapshot()
    run = sweep(account, document, *flags)
    assert run.returncode == 0, run
    assert account.snapshot() == before
    return run


def test_plain_strings_match_identifiers(account):
    q_del, q_keep = account.queue("pl-del"), account.queue("pl-keep")
    t_arn, t_label = account.topic("pl-arn-only"), account.topic("pl-labelled")
    sub = account.subscription(t_label, q_keep)
    k_keep, k_del = account.key(), account.key()
    s_keep, s_del = account.secret("pl-secret-keep"), account.secret("pl-secret-del")
    document = config(account, ["SQSQueue", "SNSTopic", "SNSSubscription", "KMSKey", "SecretsManagerSecret"], {
        "SQSQueue": [q_keep],
        "SNSTopic": [t_arn, f"TopicARN: {t_label}"],
        "SNSSubscription": [f"Owner: {account.id} ARN: {sub}"],
        "KMSKey": [k_keep],
        "SecretsManagerSecret": [s_keep, "pl-secret-del"],
    })
    run = classify(account, document)
    assert run.report() == expect(
        removes("SQSQueue", q_del), keeps("SQSQueue", q_keep),
        removes("SNSTopic", f"TopicARN: {t_arn}"), keeps("SNSTopic", f"TopicARN: {t_label}"),
        keeps("SNSSubscription", f"Owner: {account.id} ARN: {sub}"),
        keeps("KMSKey", k_keep), removes("KMSKey", k_del),
        keeps("SecretsManagerSecret", s_keep), removes("SecretsManagerSecret", s_del),
        account.aws_managed("KMSKey"),
    ), run


def test_property_operators(account):
    urls = {n: account.queue(n) for n in ("op-del", "op-keep", "op-x1", "op-x22")}
    names = [account.parameter(n) for n in ("/op/a", "/op/b", "/other/a", "/op/deep/c")]
    document = config(account, ["SQSQueue", "SSMParameter"], {
        "SQSQueue": [
            {"property": "QueueURL", "type": "contains", "value": "-ke"},
            {"property": "QueueURL", "type": "glob", "value": "*-x?"},
            {"property": "QueueURL", "type": "glob", "value": "http://*/*/op-x??"},
        ],
        "SSMParameter": [
            {"property": "Name", "type": "regex", "value": "^/other"},
            {"property": "Name", "value": "/op"},
            {"property": "Name", "type": "exact", "value": "/op/b"},
            {"type": "glob", "value": "/op/*/c"},
        ],
    })
    run = classify(account, document)
    assert run.report() == expect(
        removes("SQSQueue", urls["op-del"], urls["op-x1"]), keeps("SQSQueue", urls["op-keep"], urls["op-x22"]),
        removes("SSMParameter", "/op/a"), keeps("SSMParameter", "/op/b", "/other/a", "/op/deep/c"),
    ), run
    assert names


def test_unanchored_regex_on_identifier(account):
    urls = [account.queue(n) for n in ("rx-alpha", "rx-beta", "rx-gamma")]
    document = config(account, ["SQSQueue"], {"SQSQueue": [{"type": "regex", "value": "et|mm"}]})
    run = classify(account, document)
    assert run.report() == expect(removes("SQSQueue", urls[0]), keeps("SQSQueue", urls[1], urls[2])), run


def test_tag_filters_and_invert(account):
    a1 = account.parameter("/tg/a1", tags={"team": "a", "env": "dev"})
    a2 = account.parameter("/tg/a2", tags={"team": "a", "env": "prod"})
    b1 = account.parameter("/tg/b1", tags={"team": "b"})
    none = account.parameter("/tg/none")
    document = config(account, ["SSMParameter"], {"SSMParameter": [
        {"property": "tag:team", "value": "a", "invert": True},
    ]})
    run = classify(account, document)
    assert run.report() == expect(removes("SSMParameter", a1, a2), keeps("SSMParameter", b1, none)), run
    document = config(account, ["SSMParameter"], {"SSMParameter": [
        {"property": "tag:env", "value": "prod"}, {"property": "tag:team", "value": "b", "invert": "true"},
    ]})
    run = classify(account, document)
    assert run.report() == expect(removes("SSMParameter", b1), keeps("SSMParameter", a1, a2, none)), run


def test_type_specific_properties(account):
    target = account.queue("pp-target")
    dev, prod = account.alarm("pp-dev", tags={"env": "dev"}), account.alarm("pp-prod", tags={"env": "prod"})
    account.schedule("pp-a", target)
    account.schedule("pp-b", target, group="pp-keepers")
    core = account.key(tags={"team": "core"}, alias="alias/pp-core")
    busy = account.key(alias="alias/pp-busy")
    account.grant(busy, "rds.amazonaws.com")
    topic = account.topic("pp-topic")
    sub_keep, sub_del = account.subscription(topic, target), account.subscription(account.topic("pp-other"), target)
    document = config(account, ["CloudWatchAlarm", "SchedulerSchedule", "KMSKey", "KMSAlias", "SNSSubscription"], {
        "CloudWatchAlarm": [{"property": "tag:env", "value": "prod"}],
        "SchedulerSchedule": [{"property": "GroupName", "value": "pp-keepers"}],
        "KMSAlias": [{"property": "key:tag:team", "value": "core"}],
        "KMSKey": [{"property": "InUse", "value": "true"}],
        "SNSSubscription": [{"property": "TopicARN", "value": topic}],
    })
    run = classify(account, document)
    assert run.report() == expect(
        removes("CloudWatchAlarm", dev), keeps("CloudWatchAlarm", prod),
        removes("SchedulerSchedule", "pp-a"), keeps("SchedulerSchedule", "pp-b"),
        keeps("KMSAlias", "alias/pp-core"), removes("KMSAlias", "alias/pp-busy"),
        removes("KMSKey", core), keeps("KMSKey", busy),
        keeps("SNSSubscription", f"Owner: {account.id} ARN: {sub_keep}"),
        removes("SNSSubscription", f"Owner: {account.id} ARN: {sub_del}"),
        account.aws_managed(),
    ), run
    document = config(account, ["KMSKey", "CloudWatchAlarm"], {
        "KMSKey": [{"property": "Alias", "value": "alias/pp-core"}, {"property": "Manager", "value": "CUSTOMER", "invert": True}],
        "CloudWatchAlarm": [{"property": "Type", "value": "MetricAlarm"}],
    })
    run = classify(account, document)
    assert run.report() == expect(
        keeps("KMSKey", core), removes("KMSKey", busy), keeps("CloudWatchAlarm", dev, prod),
        account.aws_managed("KMSKey"),
    ), run


def dated(account):
    now = datetime.datetime.now(datetime.timezone.utc)
    recent = now - datetime.timedelta(hours=2)
    values = {
        "old-dash": "2020-01-01",
        "old-slash": "2020/01/01",
        "old-unix": "1577836800",
        "recent-utc": recent.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "recent-offset": recent.astimezone(datetime.timezone(datetime.timedelta(hours=5))).isoformat(timespec="seconds"),
        "future": "2999-12-31",
    }
    for name, value in values.items():
        account.parameter(f"/dt/{name}", tags={"created": value})
    account.parameter("/dt/untagged")
    return sorted(values) + ["untagged"]


OLD = ["old-dash", "old-slash", "old-unix"]
RECENT = ["recent-utc", "recent-offset"]

# (type, value, invert, kept)
DATES = {
    "older-than-24h": ("dateOlderThan", "24h", False, RECENT + ["future"]),
    "older-than-minus-24h": ("dateOlderThan", "-24h", False, ["future"]),
    "older-than-now-minus-24h": ("dateOlderThanNow", "-24h", False, OLD),
    "older-than-now-24h": ("dateOlderThanNow", "24h", False, OLD + RECENT),
    "older-than-now-minus-24h-inverted": ("dateOlderThanNow", "-24h", True, RECENT + ["future", "untagged"]),
}


@pytest.mark.parametrize("case", sorted(DATES))
def test_date_filters(account, case):
    kind, value, invert, kept = DATES[case]
    names = dated(account)
    rule = {"property": "tag:created", "type": kind, "value": value, **({"invert": True} if invert else {})}
    run = classify(account, config(account, ["SSMParameter"], {"SSMParameter": [rule]}))
    assert run.report() == expect(
        keeps("SSMParameter", *(f"/dt/{n}" for n in kept)),
        removes("SSMParameter", *(f"/dt/{n}" for n in names if n not in kept)),
    ), run


def test_date_filter_on_creation_time(account):
    group = account.log_group("/dt/created-now")
    for kind, value, kept in (("dateOlderThan", "1h", True), ("dateOlderThan", "-1h", False),
                              ("dateOlderThanNow", "1h", True), ("dateOlderThanNow", "-1h", False)):
        rule = {"property": "CreationTime", "type": kind, "value": value}
        run = classify(account, config(account, ["CloudWatchLogsLogGroup"], {"CloudWatchLogsLogGroup": [rule]}))
        assert run.report() == (keeps if kept else removes)("CloudWatchLogsLogGroup", group), (kind, value, str(run))


def test_globals_and_presets(account):
    p = {n: account.parameter(f"/pr/{n}", tags={"keep": "yes"} if n == "tagged" else None)
         for n in ("tagged", "preset-a", "preset-b", "own", "del", "unused")}
    q_tagged, q_del = account.queue("pr-tagged", tags={"keep": "yes"}), account.queue("pr-del")
    lg_own, lg_del = account.log_group("/pr/own"), account.log_group("/pr/lg-del")
    document = config(account, ["SSMParameter", "SQSQueue", "CloudWatchLogsLogGroup"],
                      {"SSMParameter": ["/pr/own"]},
                      account_settings={"presets": ["keep-tagged", "a", "b"]},
                      presets={
                          "keep-tagged": {"filters": {"__global__": [{"property": "tag:keep", "value": "yes"}]}},
                          "a": {"filters": {"SSMParameter": ["/pr/preset-a"]}},
                          "b": {"filters": {"SSMParameter": [{"property": "Name", "value": "/pr/preset-b"}],
                                            "__global__": ["/pr/own"]}},
                          "unused": {"filters": {"SSMParameter": ["/pr/unused"]}},
                      })
    run = classify(account, document)
    expected = expect(
        keeps("SSMParameter", p["tagged"], p["preset-a"], p["preset-b"], p["own"]),
        removes("SSMParameter", p["del"], p["unused"]),
        keeps("SQSQueue", q_tagged), removes("SQSQueue", q_del),
        keeps("CloudWatchLogsLogGroup", lg_own), removes("CloudWatchLogsLogGroup", lg_del),
    )
    assert run.report() == expected, run
    before = account.snapshot()
    run = sweep(account, document, "--no-dry-run")
    assert run.returncode == 0, run
    assert run.report() == expected, run
    assert account.snapshot() == {
        **before,
        "parameters": sorted([p["tagged"], p["preset-a"], p["preset-b"], p["own"]]),
        "queues": [q_tagged],
        "log_groups": [lg_own],
    }


def grouped(account):
    return {
        "a-dev": account.parameter("/gr/a-dev", tags={"team": "a", "env": "dev"}),
        "a-prod": account.parameter("/gr/a-prod", tags={"team": "a", "env": "prod"}),
        "b-dev": account.parameter("/gr/b-dev", tags={"team": "b", "env": "dev"}),
        "none": account.parameter("/gr/none"),
    }


GROUPS = {
    "two-groups": [
        {"property": "tag:team", "value": "a", "group": "owner"},
        {"property": "tag:env", "value": "dev", "group": "stage"},
    ],
    "named-and-default-group": [
        {"property": "tag:team", "value": "a", "group": "owner"},
        {"property": "tag:env", "value": "dev"},
        {"property": "Name", "type": "glob", "value": "/gr/n*"},
    ],
    "alternatives-within-group": [
        {"property": "tag:team", "value": "b", "group": "owner"},
        {"property": "Name", "type": "contains", "value": "none", "group": "owner"},
    ],
}

# case -> (kept without the feature flag, kept with it)
GROUP_KEPT = {
    "two-groups": (["a-dev", "a-prod", "b-dev"], ["a-dev"]),
    "named-and-default-group": (["a-dev", "a-prod", "b-dev", "none"], ["a-dev"]),
    "alternatives-within-group": (["b-dev", "none"], ["b-dev", "none"]),
}


@pytest.mark.parametrize("flagged", [False, True], ids=["without-flag", "with-flag"])
@pytest.mark.parametrize("case", sorted(GROUPS))
def test_filter_groups(account, case, flagged):
    names = grouped(account)
    kept = GROUP_KEPT[case][1 if flagged else 0]
    document = config(account, ["SSMParameter"], {"SSMParameter": GROUPS[case]})
    run = classify(account, document, *(["--feature-flag", "filter-groups"] if flagged else []))
    assert run.report() == expect(
        keeps("SSMParameter", *(names[n] for n in kept)),
        removes("SSMParameter", *(names[n] for n in names if n not in kept)),
    ), run


def test_global_filters_join_type_groups(account):
    names = grouped(account)
    dev_queue, prod_queue = account.queue("gr-dev", tags={"env": "dev"}), account.queue("gr-prod", tags={"env": "prod"})
    document = config(account, ["SSMParameter", "SQSQueue"], {
        "__global__": [{"property": "tag:env", "value": "dev", "group": "stage"}],
        "SSMParameter": [{"property": "tag:team", "value": "b", "group": "owner"}],
    })
    run = classify(account, document, "--feature-flag", "filter-groups")
    assert run.report() == expect(
        keeps("SSMParameter", names["b-dev"]),
        removes("SSMParameter", names["a-dev"], names["a-prod"], names["none"]),
        keeps("SQSQueue", dev_queue), removes("SQSQueue", prod_queue),
    ), run
