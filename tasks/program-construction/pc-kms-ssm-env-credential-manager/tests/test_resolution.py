"""Resolving many secrets at once: batch fetches, missing values, defaults, if_missing precedence and filters."""

import json

import boto3
import pytest

from support import child_env, failed, kms_ciphertext, ok


def base(kms_key, ps_prefix, sm_prefix, top=""):
    return f"""\
root = true
{top}
[providers.kms]
type = "aws-kms"
key_id = "{kms_key}"
region = "us-east-1"

[providers.ps]
type = "aws-ps"
region = "us-east-1"
prefix = "{ps_prefix}"

[providers.sm]
type = "aws-sm"
region = "us-east-1"
prefix = "{sm_prefix}"

[secrets]
"""


@pytest.fixture
def many(kms_key, ps_prefix, sm_prefix):
    """12 parameters (more than one GetParameters chunk) and SM secrets shared by several keys."""
    ssm = boto3.client("ssm")
    for i in range(12):
        ssm.put_parameter(Name=f"{ps_prefix}p{i:02d}", Value=f"ps-{i:02d}", Type="SecureString")
    ssm.put_parameter(Name=ps_prefix + "plainstr", Value="str-type", Type="String")
    sm = boto3.client("secretsmanager")
    sm.create_secret(Name=sm_prefix + "json", SecretString=json.dumps({"user": "admin", "password": "hunter2"}))
    sm.create_secret(Name=sm_prefix + "raw", SecretString="raw-sm")
    lines = "".join(f'P{i:02d} = {{ provider = "ps", value = "p{i:02d}" }}\n' for i in range(12))
    lines += 'PSTR = { provider = "ps", value = "plainstr" }\n'
    lines += 'SUSER = { provider = "sm", value = "json", json_path = "user" }\n'
    lines += 'SPASS = { provider = "sm", value = "json", json_path = "password" }\n'
    lines += 'SRAW = { provider = "sm", value = "raw" }\n'
    lines += 'SRAW_AGAIN = { provider = "sm", value = "raw" }\n'
    lines += f'KTOK = {{ provider = "kms", value = "{kms_ciphertext("kms-value", kms_key)}" }}\n'
    lines += 'PLAIN = { default = "plain" }\n'
    expected = {f"P{i:02d}": f"ps-{i:02d}" for i in range(12)}
    expected.update({"PSTR": "str-type", "SUSER": "admin", "SPASS": "hunter2", "SRAW": "raw-sm", "SRAW_AGAIN": "raw-sm",
                     "KTOK": "kms-value", "PLAIN": "plain"})
    return lines, expected


def test_exec_resolves_every_provider_in_batches(project, kms_key, ps_prefix, sm_prefix, many):
    lines, expected = many
    project.write("fnox.toml", base(kms_key, ps_prefix, sm_prefix) + lines)
    values, _ = child_env(project, names=list(expected))
    assert values == expected
    doc = json.loads(ok(project.run("export", "-f", "json")).stdout)
    assert doc["secrets"] == expected
    assert ok(project.run("check")).returncode == 0


MISSING = """\
PMISS = { provider = "ps", value = "nope" }
PMISS_DEFAULT = { provider = "ps", value = "nope2", default = "pdef" }
SMISS = { provider = "sm", value = "gone" }
SMISS_DEFAULT = { provider = "sm", value = "gone2", default = "sdef" }
"""


def test_missing_values_are_dropped_under_warn(project, kms_key, ps_prefix, sm_prefix, many):
    lines, expected = many
    project.write("fnox.toml", base(kms_key, ps_prefix, sm_prefix) + lines + MISSING)
    names = [*expected, "PMISS", "PMISS_DEFAULT", "SMISS", "SMISS_DEFAULT"]
    want = {**expected, "PMISS_DEFAULT": "pdef", "SMISS_DEFAULT": "sdef"}
    values, _ = child_env(project, names=names, env={"PMISS": "inherited"})
    assert values == {**want, "PMISS": "inherited"}
    doc = json.loads(ok(project.run("export", "-f", "json")).stdout)
    assert doc["secrets"] == want
    env = json.loads(ok(project.run("env", "--json")).stdout)
    assert env["set"] == want
    assert sorted(env["missing"]) == ["PMISS", "SMISS"]
    assert set(env["remove"]) & set(names) == set()  # missing secrets keep an inherited value
    assert ok(project.run("check")).returncode == 0
    # get is strict for the missing secret itself but uses a configured default.
    assert failed(project.run("get", "PMISS")).stdout == ""
    assert failed(project.run("get", "SMISS")).stdout == ""
    assert ok(project.run("get", "PMISS_DEFAULT")).stdout == "pdef\n"
    assert ok(project.run("get", "SMISS_DEFAULT")).stdout == "sdef\n"


def test_cli_if_missing_error_fails_everything(project, kms_key, ps_prefix, sm_prefix, many):
    lines, _ = many
    project.write("fnox.toml", base(kms_key, ps_prefix, sm_prefix) + lines + MISSING)
    marker = project.path("ran")
    result = failed(project.run("--if-missing", "error", "exec", "--", "touch", str(marker)))
    assert not marker.exists() and result.stdout == ""
    assert failed(project.run("--if-missing", "error", "export", "-f", "json")).stdout == ""
    failed(project.run("--if-missing", "error", "check"))
    err = failed(project.run("--if-missing", "error", "env", "--json"))
    assert "error" in json.loads(err.stdout)
    # With only defaults missing, error mode still succeeds through the defaults.
    project.write("fnox.toml", base(kms_key, ps_prefix, sm_prefix) + lines +
                  'PMISS_DEFAULT = { provider = "ps", value = "nope2", default = "pdef" }\n')
    values, _ = child_env(project, "--if-missing", "error", names=["PMISS_DEFAULT", "P11"])
    assert values == {"PMISS_DEFAULT": "pdef", "P11": "ps-11"}


@pytest.mark.parametrize(("top", "secret", "cli", "fails"), [
    ('if_missing = "error"', "", None, True),
    ('if_missing = "error"', 'if_missing = "warn"', None, False),
    ('if_missing = "ignore"', 'if_missing = "error"', None, True),
    ("", 'if_missing = "error"', "warn", False),
    ("", 'if_missing = "error"', "ignore", False),
    ('if_missing = "ignore"', "", "error", True),
    ("", "", None, False),
], ids=["top-error", "secret-warn-over-top-error", "secret-error-over-top-ignore", "cli-warn-over-secret",
        "cli-ignore-over-secret", "cli-error-over-top", "default-warn"])
def test_if_missing_precedence(project, kms_key, ps_prefix, sm_prefix, top, secret, cli, fails):
    boto3.client("ssm").put_parameter(Name=ps_prefix + "present", Value="here", Type="SecureString")
    extra = f", {secret}" if secret else ""
    project.write("fnox.toml", base(kms_key, ps_prefix, sm_prefix, top) +
                  f'GONE = {{ provider = "ps", value = "absent"{extra} }}\nOK = {{ provider = "ps", value = "present" }}\n')
    flags = ["--if-missing", cli] if cli else []
    script = 'printf "%s|%s" "${GONE-unset}" "$OK"'
    exec_result = project.run(*flags, "exec", "--", "bash", "-c", script)
    check = project.run(*flags, "check")
    if fails:
        failed(exec_result)
        failed(check)
    else:
        assert ok(exec_result).stdout == "unset|here"
        ok(check)


def test_check_reports_without_printing_values(project, kms_key, ps_prefix, sm_prefix, many):
    lines, expected = many
    project.write("fnox.toml", base(kms_key, ps_prefix, sm_prefix) + lines + MISSING)
    for args in ([], ["-a"], ["--if-missing", "error"], ["--if-missing", "ignore"]):
        result = project.run(*args, "check")
        for value in ("ps-07", "hunter2", "kms-value", "raw-sm"):
            assert value not in result.stdout + result.stderr
    project.write("fnox.toml", base(kms_key, ps_prefix, sm_prefix) + lines + 'REQ = { provider = "sm", value = "gone", if_missing = "error" }\n')
    failed(project.run("check"))
    ok(project.run("--if-missing", "ignore", "check"))


def test_exec_only_and_except_filters(project, kms_key, ps_prefix, sm_prefix, many):
    lines, expected = many
    project.write("fnox.toml", base(kms_key, ps_prefix, sm_prefix) + lines +
                  'BROKEN = { provider = "ps", value = "absent", if_missing = "error" }\n')
    failed(project.run("exec", "--", "true"))
    names = [*expected, "BROKEN"]
    values, _ = child_env(project, names=names, flags=["--only", "SUSER,SRAW", "--only", "P0*"])
    assert values == {"SUSER": "admin", "SRAW": "raw-sm", **{f"P{i:02d}": f"ps-{i:02d}" for i in range(10)}}
    values, _ = child_env(project, names=names, flags=["--except", "BROKEN", "--except", "P*"])
    assert values == {k: v for k, v in expected.items() if not k.startswith("P")}
    # A filter that names something undeclared, or matches nothing, is an error.
    failed(project.run("exec", "--only", "NOSUCH", "--", "true"))
    failed(project.run("exec", "--only", "ZZ*", "--", "true"))
    # Filters do not bypass if_missing for selected secrets.
    failed(project.run("exec", "--only", "BROKEN,PLAIN", "--", "true"))


def test_json_path_failure_is_not_a_missing_value(project, kms_key, ps_prefix, sm_prefix):
    boto3.client("secretsmanager").create_secret(Name=sm_prefix + "json", SecretString='{"user": "admin"}')
    boto3.client("secretsmanager").create_secret(Name=sm_prefix + "text", SecretString="not json")
    project.write("fnox.toml", base(kms_key, ps_prefix, sm_prefix) +
                  'USER = { provider = "sm", value = "json", json_path = "user" }\n'
                  'BAD = { provider = "sm", value = "json", json_path = "absent" }\n')
    failed(project.run("exec", "--", "true"))
    failed(project.run("--if-missing", "ignore", "exec", "--", "true"))
    values, _ = child_env(project, names=["USER", "BAD"], flags=["--except", "BAD"])
    assert values == {"USER": "admin"}
    project.write("fnox.toml", base(kms_key, ps_prefix, sm_prefix) +
                  'NOT_JSON = { provider = "sm", value = "text", json_path = "user" }\n')
    assert failed(project.run("get", "NOT_JSON")).stdout == ""


def test_provider_field_from_secret(project, kms_key, ps_prefix, sm_prefix):
    boto3.client("ssm").put_parameter(Name=ps_prefix + "team/X", Value="via-ref", Type="SecureString")
    project.write("fnox.toml", f"""\
root = true
[providers.ps]
type = "aws-ps"
region = "us-east-1"
prefix = {{ secret = "PS_PREFIX" }}

[secrets]
PS_PREFIX = {{ default = "{ps_prefix}team/" }}
VIA = {{ provider = "ps", value = "X" }}
""")
    assert ok(project.run("get", "VIA")).stdout == "via-ref\n"
    values, _ = child_env(project, names=["VIA", "PS_PREFIX"])
    assert values == {"VIA": "via-ref", "PS_PREFIX": ps_prefix + "team/"}


def test_providerless_value_resolves_through_default_provider(project, kms_keys):
    k1, k2 = kms_keys()["KeyId"], kms_keys()["KeyId"]
    ct = kms_ciphertext("decrypted-x", k1)
    two = (f'[providers.kms]\ntype = "aws-kms"\nkey_id = "{k1}"\nregion = "us-east-1"\n\n'
           f'[providers.kms2]\ntype = "aws-kms"\nkey_id = "{k2}"\nregion = "us-east-1"\n\n')
    # default_provider decrypts a value that names no provider; a literal is not used as plaintext.
    project.write("fnox.toml", 'root = true\ndefault_provider = "kms"\n' + two +
                  f'[secrets]\nX = {{ value = "{ct}" }}\nLIT = {{ value = "literal" }}\n'
                  'LIT_D = { value = "literal", default = "dflt" }\n')
    assert ok(project.run("get", "X")).stdout == "decrypted-x\n"
    assert failed(project.run("get", "LIT")).stdout == ""
    assert ok(project.run("get", "LIT_D")).stdout == "dflt\n"
    assert json.loads(ok(project.run("export", "-f", "json")).stdout)["secrets"] == {"X": "decrypted-x", "LIT_D": "dflt"}
    env = json.loads(ok(project.run("env", "--json")).stdout)
    assert env["set"] == {"X": "decrypted-x", "LIT_D": "dflt"} and env["missing"] == ["LIT"]
    # A single provider acts as the default.
    project.write("fnox.toml", 'root = true\n' + two.split("[providers.kms2]")[0] +
                  f'[secrets]\nX = {{ value = "{ct}" }}\n')
    assert ok(project.run("get", "X")).stdout == "decrypted-x\n"
    # Several providers and no default_provider: such values are missing unless a default is set.
    project.write("fnox.toml", 'root = true\n' + two +
                  f'[secrets]\nX = {{ value = "{ct}" }}\nY = {{ value = "literal" }}\n'
                  'YD = { value = "literal", default = "literal" }\nZ = { value = "other", default = "zdef" }\n')
    for name in ("X", "Y"):
        missing = ok(project.run("get", name))
        assert missing.stdout == ""
    assert ok(project.run("get", "YD")).stdout == "literal\n"
    assert ok(project.run("get", "Z")).stdout == "zdef\n"
    assert json.loads(ok(project.run("export", "-f", "json")).stdout)["secrets"] == {"YD": "literal", "Z": "zdef"}
    env = json.loads(ok(project.run("env", "--json")).stdout)
    assert env["set"] == {"YD": "literal", "Z": "zdef"} and sorted(env["missing"]) == ["X", "Y"]
    values, _ = child_env(project, names=["X", "Y", "YD", "Z"], env={"Y": "inherited"})
    assert values == {"Y": "inherited", "YD": "literal", "Z": "zdef"}
