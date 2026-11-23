"""The graded timelines. Each scenario seeds hand-managed entries, drives syncctl through a fixed
sequence of runs (one of them killed by the proxy right after Floci applied its 2nd create; in the
later ones the first reply to each write is lost after Floci applied it, so the SDK retries), and
snapshots the emulator after every run. The test modules assert over the snapshots.
"""

from __future__ import annotations

import secrets
import hashlib
from pathlib import Path

from cdk_seed.cfn_deploy import deploy
from dataclasses import dataclass, field

from botocore.exceptions import ClientError

import harness

OWNER_TAG = "syncctl:sync-id"
KILL_AFTER_NTH_CREATE = 2


def _val(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(6)}"


@dataclass
class Step:
    run: harness.RunResult
    state: dict
    expect_rc: int | None = None
    error: str | None = None


@dataclass
class Timeline:
    seeds: dict = field(default_factory=dict)
    steps: dict[str, Step] = field(default_factory=dict)
    facts: dict = field(default_factory=dict)
    error: str | None = None


# --------------------------------------------------------------------------- Parameter Store, path "/"

def run_parameter_store_root(runner: harness.Runner) -> Timeline:
    tl = Timeline()
    ssm = harness.direct_client("ssm")
    harness.reset_ssm()

    payments = {
        "STRIPE_API_KEY": _val("sk_live"),
        "STRIPE_WEBHOOK_SECRET": _val("whsec"),
        "PAYPAL_CLIENT_SECRET": _val("pp"),
        "ADYEN_HMAC_KEY": _val("hmac"),
    }
    checkout = {
        "CHECKOUT_SIGNING_KEY": _val("sign"),
        "CHECKOUT_REDIS_URL": "redis://checkout-cache.internal:6379/0",
        "FRAUD_SCORE_THRESHOLD": "0.82",
    }

    # Hand-managed parameters at the root, as ops creates them: some tagged, many not.
    seeds = [
        # Created by hand before payments was onboarded: same name and value, no tags.
        ("/STRIPE_WEBHOOK_SECRET", payments["STRIPE_WEBHOOK_SECRET"], "SecureString", None),
        ("/PAGERDUTY_TOKEN", _val("pd"), "SecureString", {"owner": "ops"}),
        ("/LEGACY_BILLING_URL", "https://billing-v1.internal/api", "String", {"owner": "ops"}),
        ("/DATADOG_API_KEY", _val("dd"), "SecureString", None),
        ("/SENTRY_DSN", "https://abc123@o1.ingest.sentry.io/42", "String", {"owner": "ops"}),
        ("/VENDOR_SFTP_PASSWORD", _val("sftp"), "SecureString", {"owner": "ops", "rotation": "manual"}),
        ("/STATUSPAGE_KEY", _val("sp"), "String", None),
        ("/FRAUD_SCORE_THRESHOLD", "0.75", "String", {"owner": "risk"}),
        ("/ops/oncall/ROTATION", "weekly", "String", {"owner": "ops"}),
    ]
    string_seeds = [entry for entry in seeds if entry[2] == 'String']
    run_id = hashlib.sha256(seeds[0][1].encode()).hexdigest()[:16]
    deploy(harness.direct_client('cloudformation'), 'seed-ssm-' + run_id,
           Path(__file__).parent / 'cdk_seed/seed.template.json',
           **{f'Value{i}': entry[1] for i, entry in enumerate(string_seeds)})
    # CloudFormation's AWS::SSM::Parameter cannot represent SecureString.
    for name, value, ptype, tag_map in seeds:
        if ptype == 'String':
            continue
        kwargs = {"Name": name, "Value": value, "Type": ptype}
        if tag_map:
            kwargs["Tags"] = [{"Key": k, "Value": v} for k, v in tag_map.items()]
        ssm.put_parameter(**kwargs)
    # A parameter ops has edited since creating it.
    ssm.put_parameter(Name="/LEGACY_BILLING_URL", Value="https://billing-v1.internal/api/v2", Type="String",
                      Overwrite=True)
    tl.seeds = harness.ssm_snapshot()

    s1_cfg = {"sync_id": "payments-root", "destination": "ssm", "path": "/", "key_schema": None,
              "environment": "prod", "tags": {"team": "payments"}}
    s2_cfg = {"sync_id": "checkout-root", "destination": "ssm", "path": "/", "key_schema": None,
              "environment": "prod", "tags": {"team": "checkout"}}
    s1 = runner.write_sync("payments-root", s1_cfg, payments)
    s2 = runner.write_sync("checkout-root", s2_cfg, checkout)

    payments_t5 = {k: v for k, v in payments.items() if k != "PAYPAL_CLIENT_SECRET"}
    payments_t5["ADYEN_HMAC_KEY"] = _val("hmac2")
    payments_t5["KLARNA_API_KEY"] = _val("kl")
    payments_t5["GOCARDLESS_ACCESS_TOKEN"] = _val("gc")

    tl.facts = {
        "payments": payments, "payments_t5": payments_t5, "checkout": checkout,
        "s1": "payments-root", "s2": "checkout-root",
        "collision_s1": "/STRIPE_WEBHOOK_SECRET", "collision_s2": "/FRAUD_SCORE_THRESHOLD",
        # Made by hand while the timeline runs: name -> {"from": step label, "rec": state right after}.
        "late_hand": {},
    }

    def by_hand(label, name, value):
        # Ops creating a parameter with the console, untagged, with the value payments has for it.
        def act():
            try:
                ssm.put_parameter(Name=name, Value=value, Type="SecureString")
            except ClientError:
                return False
            tl.facts["late_hand"][name] = {"from": label, "rec": harness.ssm_record(ssm, name)}
            return True
        return act

    plan = [
        dict(label="T1", command="sync", cfg=s2, rc=3),
        dict(label="T2", command="sync", cfg=s1, kill_after=KILL_AFTER_NTH_CREATE, rc=None),
        dict(label="T3", command="sync", cfg=s1, rc=3),
        dict(label="T4", command="sync", cfg=s1, rc=3),
        dict(label="T4b", command="sync", cfg=s2, rc=3, lose=True),
        # PAYPAL leaves the sync and ops take it over; ADYEN rotates; two new keys, one of which ops
        # pre-create by hand while this very run is about to create it.
        dict(label="T5", command="sync", source=payments_t5, rc=3, lose=True,
             before_create={"/GOCARDLESS_ACCESS_TOKEN": by_hand("T5", "/GOCARDLESS_ACCESS_TOKEN",
                                                                payments_t5["GOCARDLESS_ACCESS_TOKEN"])},
             after_delete={"/PAYPAL_CLIENT_SECRET": by_hand("T5", "/PAYPAL_CLIENT_SECRET",
                                                            payments["PAYPAL_CLIENT_SECRET"])}),
        # payments is decommissioned; ops take over the Adyen key.
        dict(label="T6", command="remove", cfg=s1, rc=0, lose=True,
             after_delete={"/ADYEN_HMAC_KEY": by_hand("T6", "/ADYEN_HMAC_KEY", payments_t5["ADYEN_HMAC_KEY"])}),
        dict(label="T7", command="remove", cfg=s2, rc=0, lose=True),
    ]
    for step in plan:
        cfg_path = step.get("cfg")
        if "source" in step:
            cfg_path = s1 = runner.write_sync("payments-root", s1_cfg, step["source"])
        result = runner.run(step["label"], step["command"], cfg_path, kill_after=step.get("kill_after"),
                            lose_replies=step.get("lose", False), before_create=step.get("before_create"),
                            after_delete=step.get("after_delete"))
        tl.steps[step["label"]] = Step(result, harness.ssm_snapshot(), step["rc"])
    return tl


# --------------------------------------------------------------------------- Secrets Manager

def run_secrets_manager(runner: harness.Runner) -> Timeline:
    tl = Timeline()
    sm = harness.direct_client("secretsmanager")
    harness.reset_sm()

    app = {
        "API_TOKEN": _val("tok"),
        "DB_PASSWORD": _val("dbpw"),
        "CACHE_URL": "redis://app-cache.internal:6379/1",
        "QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/000000000000/app-jobs",
        "SENTRY_DSN": "https://f00d@o1.ingest.sentry.io/77",
    }
    reports = {"DAILY_SALES_BUCKET": "s3://reports-daily-sales", "WAREHOUSE_DSN": _val("dsn")}

    seeds = [
        ("app/other-1", _val("o1"), {"owner": "web"}),
        ("app/other-2", _val("o2"), {"owner": "web"}),
        ("db/creds", '{"user":"admin","password":"%s"}' % _val("p"), {"owner": "dba"}),
        ("INFISICAL_LEGACY", _val("legacy"), {"owner": "ops"}),
        # Created by hand before app-core was onboarded: same name and value, no tags.
        ("INFISICAL_DB_PASSWORD", app["DB_PASSWORD"], None),
        ("reports/legacy-export", _val("exp"), {"owner": "finance"}),
        # Ops' own secret with a name app-core will want; ops retired it in the console (below).
        ("INFISICAL_SENTRY_DSN", "https://0ld@o1.ingest.sentry.io/12", {"owner": "ops"}),
    ]
    run_id = hashlib.sha256(seeds[0][1].encode()).hexdigest()[:16]
    deploy(harness.direct_client('cloudformation'), 'seed-sm-' + run_id,
           Path(__file__).parent / 'cdk_seed/secrets.template.json',
           **{f'Value{i}': entry[1] for i, entry in enumerate(seeds)})
    sm.put_secret_value(SecretId="db/creds", SecretString='{"user":"admin","password":"%s"}' % _val("p2"))
    _console_delete(sm, "INFISICAL_SENTRY_DSN")
    tl.seeds = harness.sm_snapshot()

    app_cfg = {"sync_id": "app-core", "destination": "secretsmanager", "key_schema": "INFISICAL_{{secretKey}}",
               "environment": "prod", "mapping": "one-to-one", "tags": {"team": "core"}}
    rep_cfg = {"sync_id": "reports", "destination": "secretsmanager", "key_schema": "reports/{{secretKey}}",
               "environment": "prod", "mapping": "one-to-one", "tags": {"team": "data"}}
    rep_bundle_cfg = {"sync_id": "reports", "destination": "secretsmanager", "key_schema": None,
                      "environment": "prod", "mapping": "many-to-one", "bundle_name": "reports/bundle",
                      "tags": {"team": "data"}}

    app_t6 = {k: v for k, v in app.items() if k != "CACHE_URL"}
    app_t6["QUEUE_URL"] = "https://sqs.us-east-1.amazonaws.com/000000000000/app-jobs-v2"
    app_t6["API_TOKEN"] = _val("tok2")
    app_t9 = {"QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/000000000000/app-jobs-v3",
              "CACHE_URL": "redis://app-cache.internal:6379/3"}

    tl.facts = {"app": app, "app_t6": app_t6, "app_t9": app_t9, "reports": reports,
                "collisions": ["INFISICAL_DB_PASSWORD", "INFISICAL_SENTRY_DSN"], "bundle": "reports/bundle",
                "console_deleted": {},
                # Made by hand while the timeline runs: name -> {"from": step label, "rec": state right after}.
                "late_hand": {}}

    def take_over(label, name, value, owner):
        # A team re-creating, by hand, a secret a sync just dropped, as soon as the name is free.
        def act():
            try:
                sm.create_secret(Name=name, SecretString=value, Tags=[{"Key": "owner", "Value": owner}])
            except ClientError:
                return False
            tl.facts["late_hand"][name] = {"from": label, "rec": harness.sm_record(sm, name)}
            return True
        return act

    def console_delete(label, *names):
        # On-call deleting synced secrets in the console by mistake, between two runs.
        def act():
            for name in names:
                _console_delete(sm, name)
            tl.facts["console_deleted"][label] = list(names)
        return act

    a = runner.write_sync("app-core", app_cfg, app)
    r = runner.write_sync("reports", rep_cfg, reports)
    plan = [
        dict(label="T1", command="sync", cfg=lambda: r, rc=0),
        dict(label="T2", command="sync", cfg=lambda: a, kill_after=KILL_AFTER_NTH_CREATE, rc=None),
        dict(label="T3", command="sync", cfg=lambda: a, rc=3),
        dict(label="T4", command="sync", cfg=lambda: a, rc=3),
        # reports switches to a bundle; the data team keeps a per-key copy of WAREHOUSE_DSN for a
        # consumer that has not moved to the bundle yet.
        dict(label="T5", command="sync", cfg=lambda: runner.write_sync("reports", rep_bundle_cfg, reports), rc=0,
             lose=True, after_delete={"reports/WAREHOUSE_DSN": take_over(
                 "T5", "reports/WAREHOUSE_DSN", reports["WAREHOUSE_DSN"], "data-eng")}),
        # API_TOKEN is still in the source (rotated); CACHE_URL is dropped from it in this run.
        dict(label="T6", command="sync", cfg=lambda: runner.write_sync("app-core", app_cfg, app_t6), rc=3, lose=True,
             before=console_delete("T6", "INFISICAL_API_TOKEN", "INFISICAL_CACHE_URL")),
        # app-core is decommissioned; the platform team takes over its API token.
        dict(label="T7", command="remove", cfg=lambda: a, rc=0, lose=True,
             before=console_delete("T7", "INFISICAL_QUEUE_URL"),
             after_delete={"INFISICAL_API_TOKEN": take_over(
                 "T7", "INFISICAL_API_TOKEN", app_t6["API_TOKEN"], "platform")}),
        dict(label="T8", command="remove", cfg=lambda: runner.write_sync("reports", rep_bundle_cfg, reports), rc=0,
             lose=True, before=console_delete("T8", "reports/bundle")),
        dict(label="T9", command="sync", cfg=lambda: runner.write_sync("app-core", app_cfg, app_t9), rc=0, lose=True),
        dict(label="T9r", command="remove", cfg=lambda: a, rc=0, lose=True),
    ]
    for step in plan:
        if step.get("before") is not None:
            step["before"]()
        result = runner.run(step["label"], step["command"], step["cfg"](), kill_after=step.get("kill_after"),
                            lose_replies=step.get("lose", False), after_delete=step.get("after_delete"))
        tl.steps[step["label"]] = Step(result, harness.sm_snapshot(), step["rc"])
    return tl


def _console_delete(sm, name: str):
    # What the Secrets Manager console does: schedule the deletion with a recovery window. A run
    # that already lost the secret (or left it scheduled) leaves nothing to delete; the checks
    # after the next run report that.
    try:
        sm.delete_secret(SecretId=name, RecoveryWindowInDays=7)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in ("ResourceNotFoundException",
                                                               "InvalidRequestException"):
            raise
