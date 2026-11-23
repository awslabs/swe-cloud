"""exec: the child's complete environment, its process status and its standard streams."""

from conftest import ACCOUNT, PASSPHRASE, VENUE_CHILD, assert_lifetime, identity

STALE = {
    "AWS_PROFILE": "stale-profile",
    "AWS_DEFAULT_PROFILE": "stale-default",
    "AWS_SESSION_TOKEN": "stale-token",
    "AWS_SECURITY_TOKEN": "stale-security-token",
    "AWS_ACCOUNT_ID": "111111111111",
    "AWS_SDK_LOAD_CONFIG": "1",
    "AWS_CREDENTIAL_FILE": "/nonexistent/credentials",
    "KEEP_ME": "sentinel value",
}


def inherited(vault, **overrides):
    """What the child inherits untouched from the invocation environment."""
    return {**VENUE_CHILD, "HOME": str(vault.home), "AWS_VAULT_BACKEND": "file", "AWS_VAULT_FILE_PASSPHRASE": PASSPHRASE, "KEEP_ME": "sentinel value", **overrides}


def without_window(child):
    return {k: v for k, v in child.items() if k != "_window"}


def test_child_environment_for_a_session(vault, aws):
    user = aws.user()
    vault.write_config("[profile base]\nregion = us-west-2\n")
    vault.add("base", user)
    session = vault.export("base")
    child = vault.exec_env("base", env={**STALE, "AWS_REGION": "", "AWS_DEFAULT_REGION": ""})
    assert without_window(child) == inherited(
        vault,
        AWS_ACCESS_KEY_ID=session["AccessKeyId"],
        AWS_SECRET_ACCESS_KEY=session["SecretAccessKey"],
        AWS_SESSION_TOKEN=session["SessionToken"],
        AWS_CREDENTIAL_EXPIRATION=session["Expiration"],
        AWS_REGION="us-west-2",
        AWS_DEFAULT_REGION="us-west-2",
        AWS_VAULT="base",
    )


def test_child_environment_without_session(vault, aws):
    user = aws.user()
    vault.write_config("[profile base]\n")
    vault.add("base", user)
    child = vault.exec_env("base", "--no-session", env=STALE)
    assert without_window(child) == inherited(
        vault,
        AWS_ACCESS_KEY_ID=user.key_id,
        AWS_SECRET_ACCESS_KEY=user.secret,
        AWS_REGION="us-east-1",
        AWS_DEFAULT_REGION="us-east-1",
        AWS_VAULT="base",
    )
    assert vault.sessions() == []


def test_child_environment_for_a_role(vault, aws):
    user, role = aws.user(), aws.role()
    vault.write_config(f"[profile base]\n\n[profile ops]\nsource_profile = base\nrole_arn = {role.arn}\nendpoint_url = http://engines:4566/\n")
    vault.add("base", user)
    child = vault.exec_env("ops", "-d", "2h", env={**STALE, "AWS_ENDPOINT_URL": ""})
    assert_lifetime(child["AWS_CREDENTIAL_EXPIRATION"], 7200, child["_window"])
    assert f":assumed-role/{role.name}/" in identity(child)
    assert child["AWS_ACCESS_KEY_ID"] != user.key_id
    assert without_window(child) == inherited(
        vault,
        AWS_ENDPOINT_URL="http://engines:4566/",
        AWS_ACCESS_KEY_ID=child["AWS_ACCESS_KEY_ID"],
        AWS_SECRET_ACCESS_KEY=child["AWS_SECRET_ACCESS_KEY"],
        AWS_SESSION_TOKEN=child["AWS_SESSION_TOKEN"],
        AWS_CREDENTIAL_EXPIRATION=child["AWS_CREDENTIAL_EXPIRATION"],
        AWS_ACCOUNT_ID=ACCOUNT,
        AWS_VAULT="ops",
    )
    assert vault.sessions() == []


def test_child_can_use_its_credentials(vault, aws):
    user, role = aws.user(), aws.role()
    vault.write_config(f"[profile base]\n\n[profile ops]\nsource_profile = base\nrole_arn = {role.arn}\nrole_session_name = child-check\n")
    vault.add("base", user)
    script = "import boto3; print(boto3.client('sts').get_caller_identity()['Arn'])"
    assert vault.ok("exec", "ops", "--", "python3", "-c", script).stdout.strip() == f"arn:aws:sts::{ACCOUNT}:assumed-role/{role.name}/child-check"
    assert vault.ok("exec", "base", "--", "python3", "-c", script).stdout.strip() == user.arn


def test_exit_status_and_streams_pass_through(vault, aws):
    user = aws.user()
    vault.write_config("[profile base]\n")
    vault.add("base", user)
    result = vault.run("exec", "-n", "base", "--", "sh", "-c", 'read line; echo "got:$line"; echo "to-stderr" >&2; exit 4', stdin="hello child\n")
    assert result.returncode == 4
    assert result.stdout == "got:hello child\n"
    assert "to-stderr\n" in result.stderr
    ok = vault.run("exec", "base", "--", "sh", "-c", "printf '%s|' \"$@\"", "x", "two words", "--flag")
    assert (ok.returncode, ok.stdout) == (0, "two words|--flag|")


def test_missing_command_fails(vault, aws):
    user = aws.user()
    vault.write_config("[profile base]\n")
    vault.add("base", user)
    result = vault.run("exec", "-n", "base", "--", str(vault.root / "no-such-command"))
    assert result.returncode != 0
    assert result.stdout == ""
