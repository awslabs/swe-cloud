"""The account safety gate: refused runs change nothing; permitted variants proceed to removal."""

import uuid

import pytest

from sweep import config, sweep


def sentinels(account):
    """An included parameter (would be removed) and an excluded queue (never in scope)."""
    account.parameter("/gate/sentinel")
    account.queue("gate-untouched")
    return account.snapshot()


def suffix():
    return uuid.uuid4().hex[:8]


# (alias or None, configuration changes, extra flags)
REFUSED = {
    "no-alias": (None, {}, []),
    "empty-blocklist": ("ok-{}", {"blocklist": []}, []),
    "account-blocklisted": ("ok-{}", {"blocklist": ["999999999999", "{id}"]}, []),
    "account-not-configured": ("ok-{}", {"accounts": {"111111111111": {}}}, []),
    "account-entry-null": ("ok-{}", {"accounts": {"{id}": None}}, []),
    "alias-contains-prod": ("team-prod-{}", {}, []),
    "alias-contains-production": ("myproduction{}", {}, []),
    "extra-term": ("team-staging-{}", {"blocklist-terms": ["staging"]}, []),
    "extra-term-keeps-default": ("team-prod-{}", {"blocklist-terms": ["staging"]}, []),
    "custom-term-without-default": ("qa-team-{}", {"blocklist-terms": ["qa"], "no-blocklist-terms-default": True}, []),
    "no-alias-check-not-bypassed": (None, {"bypass-alias-check-accounts": ["111111111111"]}, ["--no-alias-check"]),
    "bypass-listed-without-flag": (None, {"bypass-alias-check-accounts": ["{id}"]}, []),
    "unknown-preset": ("ok-{}", {"accounts": {"{id}": {"presets": ["missing"]}}}, []),
    "prompt-delay-too-short": ("ok-{}", {}, ["--prompt-delay", "2"]),
}

PERMITTED = {
    "plain-alias": ("sandbox-{}", {}, []),
    "bypass-with-flag": (None, {"bypass-alias-check-accounts": ["{id}"]}, ["--no-alias-check"]),
    "bypass-with-flag-prod-alias": ("team-prod-{}", {"bypass-alias-check-accounts": ["{id}"]}, ["--no-alias-check"]),
    "default-term-dropped": ("team-prod-{}", {"no-blocklist-terms-default": True}, []),
    "upper-case-term": ("team-staging-{}", {"blocklist-terms": ["STAGING"]}, []),
    "term-not-contained": ("team-stage-{}", {"blocklist-terms": ["staging"]}, []),
}


def substitute(value, account_id):
    if isinstance(value, str):
        return value.replace("{id}", account_id)
    if isinstance(value, list):
        return [substitute(v, account_id) for v in value]
    if isinstance(value, dict):
        return {substitute(k, account_id): substitute(v, account_id) for k, v in value.items()}
    return value


def prepared(account, alias, changes):
    if alias:
        account.set_alias(alias.format(suffix()))
    document = config(account, ["SSMParameter"])
    document.update(substitute(changes, account.id))
    document["endpoints"] = document.pop("endpoints")
    return document


@pytest.mark.parametrize("case", sorted(REFUSED))
def test_refused_run_changes_nothing(bare_account, case):
    alias, changes, flags = REFUSED[case]
    document = prepared(bare_account, alias, changes)
    before = sentinels(bare_account)
    run = sweep(bare_account, document, "--no-dry-run", *flags)
    assert run.returncode != 0, run
    assert not run.report(), run
    assert bare_account.snapshot() == before


@pytest.mark.parametrize("case", sorted(PERMITTED))
def test_permitted_run_removes(bare_account, case):
    alias, changes, flags = PERMITTED[case]
    document = prepared(bare_account, alias, changes)
    before = sentinels(bare_account)
    run = sweep(bare_account, document, "--no-dry-run", *flags)
    assert run.returncode == 0, run
    assert run.report() == {("SSMParameter", "/gate/sentinel", "would remove"): 1}, run
    assert bare_account.snapshot() == {**before, "parameters": []}


def test_dry_run_through_gate_reports_without_changes(bare_account):
    document = prepared(bare_account, None, {"bypass-alias-check-accounts": ["{id}"]})
    before = sentinels(bare_account)
    run = sweep(bare_account, document, "--no-alias-check")
    assert run.returncode == 0, run
    assert run.report() == {("SSMParameter", "/gate/sentinel", "would remove"): 1}, run
    assert bare_account.snapshot() == before
