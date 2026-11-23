"""Invocation forms, dry-run default, quiet output and exit status."""

import yaml

from harness import run_program, workspace, write_fixture
from sweep import Run, config, expect, keeps, removes, sweep


def small_account(account):
    keep = account.parameter("/cli/keep", tags={"keep": "yes"})
    gone = account.parameter("/cli/gone")
    url = account.queue("cli-queue")
    filters = {"SSMParameter": [{"property": "tag:keep", "value": "yes"}]}
    return keep, gone, url, config(account, ["SSMParameter", "SQSQueue"], filters)


def test_dry_run_is_default(account):
    keep, gone, url, document = small_account(account)
    before = account.snapshot()
    run = sweep(account, document)
    assert run.returncode == 0, run
    assert run.report() == expect(keeps("SSMParameter", keep), removes("SSMParameter", gone),
                                  removes("SQSQueue", url)), run
    assert account.snapshot() == before


def test_quiet_hides_filtered_lines(account):
    keep, gone, url, document = small_account(account)
    before = account.snapshot()
    run = sweep(account, document, "--quiet")
    assert run.returncode == 0, run
    assert run.report() == expect(removes("SSMParameter", gone), removes("SQSQueue", url)), run
    assert account.snapshot() == before


def test_quiet_real_run_keeps_filtered(account):
    keep, gone, url, document = small_account(account)
    before = account.snapshot()
    run = sweep(account, document, "--quiet", "--no-dry-run")
    assert run.returncode == 0, run
    assert run.report() == expect(removes("SSMParameter", gone), removes("SQSQueue", url)), run
    assert account.snapshot() == {**before, "parameters": [keep], "queues": []}


def test_short_and_alias_forms(account):
    keep, gone, url, document = small_account(account)
    before = account.snapshot()
    with workspace() as root:
        write_fixture(root / "sweep.yaml", yaml.safe_dump(document, sort_keys=False), mode=0o644)
        completed = run_program(
            ["nuke", "-c", "sweep.yaml", "--default-region", "us-east-1", "--force", "--force-sleep", "3",
             "-q", "--run-sleep-delay", "1s", "--log-disable-colors", "--no-dry-run"],
            cwd=root, timeout=55, env={"AWS_ACCESS_KEY_ID": account.id, "AWS_SECRET_ACCESS_KEY": "test"})
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert account.snapshot() == {**before, "parameters": [keep], "queues": []}


def test_default_config_path_is_config_yaml(account):
    keep, gone, url, document = small_account(account)
    before = account.snapshot()
    with workspace() as root:
        write_fixture(root / "config.yaml", yaml.safe_dump(document, sort_keys=False), mode=0o644)
        completed = run_program(
            ["run", "--default-region", "us-east-1", "--no-prompt", "--prompt-delay", "3", "--log-disable-colors"],
            cwd=root, timeout=55, env={"AWS_ACCESS_KEY_ID": account.id, "AWS_SECRET_ACCESS_KEY": "test"})
    run = Run(completed)
    assert run.returncode == 0, run
    assert run.report() == expect(keeps("SSMParameter", keep), removes("SSMParameter", gone),
                                  removes("SQSQueue", url)), run
    assert account.snapshot() == before


def test_missing_config_file_fails(account):
    small_account(account)
    before = account.snapshot()
    with workspace() as root:
        completed = run_program(
            ["run", "--config", "absent.yaml", "--default-region", "us-east-1", "--no-prompt",
             "--prompt-delay", "3", "--no-dry-run"],
            cwd=root, timeout=55, env={"AWS_ACCESS_KEY_ID": account.id, "AWS_SECRET_ACCESS_KEY": "test"})
    assert completed.returncode != 0
    assert account.snapshot() == before


def test_nothing_to_delete_succeeds(account):
    url = account.queue("only-kept", tags={"hold": "1"})
    document = config(account, ["SQSQueue", "SSMParameter"], {"SQSQueue": [{"property": "tag:hold", "value": "1"}]})
    for flags in ((), ("--no-dry-run",)):
        run = sweep(account, document, *flags)
        assert run.returncode == 0, run
        assert run.report() == keeps("SQSQueue", url), run
    assert account.snapshot()["queues"] == [url]


def test_empty_account_succeeds(account):
    run = sweep(account, config(account, ["SQSQueue", "SSMParameter"]), "--no-dry-run")
    assert run.returncode == 0, run
    assert not run.report(), run


def test_credentials_from_flags(account):
    keep, gone, url, document = small_account(account)
    before = account.snapshot()
    with workspace() as root:
        write_fixture(root / "config.yaml", yaml.safe_dump(document, sort_keys=False), mode=0o644)
        # The environment carries the shared default credentials; the flags select the account.
        completed = run_program(
            ["run", "--config", "config.yaml", "--default-region", "us-east-1", "--no-prompt", "--prompt-delay", "3",
             "--run-sleep-delay", "1s", "--log-disable-colors", "--no-dry-run",
             "--access-key-id", account.id, "--secret-access-key", "test"],
            cwd=root, timeout=55)
    run = Run(completed)
    assert run.returncode == 0, run
    assert run.report() == expect(keeps("SSMParameter", keep), removes("SSMParameter", gone),
                                  removes("SQSQueue", url)), run
    assert account.snapshot() == {**before, "parameters": [keep], "queues": []}
