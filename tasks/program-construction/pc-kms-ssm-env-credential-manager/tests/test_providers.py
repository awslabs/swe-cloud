"""Single provider operations: where `set` stores a value, how `get` reads it back, and refused writes."""

import base64
import json

import boto3
import pytest

from support import ENDPOINT, failed, kms_ciphertext, kms_plaintext, ok, parameter, secret_string


def providers(key="", ps="", sm="", *, endpoint=False):
    ep = f'endpoint = "{ENDPOINT}"\n' if endpoint else ""
    text = "root = true\n# providers follow\n"
    if key:
        text += f'[providers.kms]\ntype = "aws-kms"\nkey_id = "{key}"\nregion = "us-east-1"\n{ep}\n'
    if ps is not None and ps != "":
        text += f'[providers.ps]\ntype = "aws-ps"\nregion = "us-east-1"\nprefix = "{ps}"\n{ep}\n'
    if sm is not None and sm != "":
        text += f'[providers.sm]\ntype = "aws-sm"\nregion = "us-east-1"\nprefix = "{sm}"\n{ep}\n'
    return text


SENTINEL = '[secrets]\n# sentinel comment\nKEEP = { default = "keep-me", description = "unrelated" }\n'


# --- aws-kms -------------------------------------------------------------------------------------

@pytest.mark.parametrize("endpoint", [False, True], ids=["env-endpoint", "config-endpoint"])
def test_kms_set_stores_inline_ciphertext(project, kms_key, endpoint):
    project.write("fnox.toml", providers(kms_key, endpoint=endpoint) + SENTINEL)
    ok(project.run("set", "API_TOKEN", "kms secret value!", "--provider", "kms"))
    text = project.read_text()
    assert "kms secret value!" not in text
    assert "# sentinel comment" in text and "# providers follow" in text
    secrets = project.config()["secrets"]
    assert set(secrets) == {"KEEP", "API_TOKEN"}
    assert secrets["KEEP"] == {"default": "keep-me", "description": "unrelated"}
    entry = secrets["API_TOKEN"]
    assert set(entry) == {"provider", "value"} and entry["provider"] == "kms"
    assert kms_plaintext(entry["value"], kms_key) == b"kms secret value!"
    assert ok(project.run("get", "API_TOKEN")).stdout == "kms secret value!\n"


def test_kms_get_decrypts_externally_encrypted_values(project, kms_keys):
    key = kms_keys()
    values = {"SIMPLE": "test-plaintext-value", "SPECIAL": '{"password":"p@ssw0rd!","key":"abc=123&xyz"}',
              "MULTILINE": "line1\nline2\nline3", "UNICODE": "zażółć gęślą jaźń ✓"}
    for key_ref in (key["KeyId"], key["Arn"]):
        lines = "".join(f'{name} = {{ provider = "kms", value = "{kms_ciphertext(v, key["KeyId"])}" }}\n'
                        for name, v in values.items())
        project.write("fnox.toml", providers(key_ref) + "[secrets]\n" + lines)
        for name, value in values.items():
            assert ok(project.run("get", name)).stdout == value + "\n"


def test_kms_alias_key_id_encrypts_under_target_key(project, kms_keys, uniq):
    key = kms_keys(alias=f"alias/sem-{uniq}")
    project.write("fnox.toml", providers(f"alias/sem-{uniq}") + SENTINEL)
    ok(project.run("set", "ALIASED", "via-alias", "-p", "kms"))
    value = project.config()["secrets"]["ALIASED"]["value"]
    assert kms_plaintext(value, key["KeyId"]) == b"via-alias"
    assert ok(project.run("get", "ALIASED")).stdout == "via-alias\n"


def test_kms_get_rejects_invalid_ciphertext(project, kms_key):
    project.write("fnox.toml", providers(kms_key) + '[secrets]\nBROKEN = { provider = "kms", value = "invalid-base64-!@#$%" }\n'
                  f'FOREIGN = {{ provider = "kms", value = "{base64.b64encode(b"not a kms blob").decode()}" }}\n')
    assert failed(project.run("get", "BROKEN")).stdout == ""
    assert failed(project.run("get", "FOREIGN")).stdout == ""


def test_kms_set_with_unknown_key_fails_without_writing(project, uniq):
    project.write("fnox.toml", providers(f"alias/sem-missing-{uniq}") + SENTINEL)
    before = project.read_bytes()
    failed(project.run("set", "WRONG_KEY", "value", "--provider", "kms"))
    assert project.read_bytes() == before


def test_kms_update_reuses_existing_provider(project, kms_keys):
    k1, k2 = kms_keys()["KeyId"], kms_keys()["KeyId"]
    text = providers(k1) + \
        f'[providers.other]\ntype = "aws-kms"\nkey_id = "{k2}"\nregion = "us-east-1"\n\n' + SENTINEL
    project.write("fnox.toml", text)
    ok(project.run("set", "ROTATED", "original", "-p", "other"))
    ok(project.run("set", "ROTATED", "updated"))
    entry = project.config()["secrets"]["ROTATED"]
    assert entry["provider"] == "other"
    assert kms_plaintext(entry["value"], k2) == b"updated"
    assert ok(project.run("get", "ROTATED")).stdout == "updated\n"


def test_kms_base64_encode_and_decode(project, kms_key):
    project.write("fnox.toml", providers(kms_key) + SENTINEL)
    ok(project.run("set", "B64", "aGVsbG8=", "--base64-encode", "-p", "kms"))
    entry = project.config()["secrets"]["B64"]
    assert kms_plaintext(entry["value"], kms_key) == b"YUdWc2JHOD0="
    assert ok(project.run("get", "B64")).stdout == "YUdWc2JHOD0=\n"
    assert ok(project.run("get", "--base64-decode", "B64")).stdout == "aGVsbG8=\n"


def test_set_from_file_and_stdin(project, kms_key):
    project.write("fnox.toml", providers(kms_key) + SENTINEL)
    project.write("value.txt", "file-content\nsecond line\n")
    ok(project.run("set", "FROM_FILE", "--from-file", "value.txt", "-p", "kms"))
    assert kms_plaintext(project.config()["secrets"]["FROM_FILE"]["value"], kms_key) == b"file-content\nsecond line\n"
    ok(project.run("set", "FROM_STDIN", "-p", "kms", stdin="piped value"))
    assert kms_plaintext(project.config()["secrets"]["FROM_STDIN"]["value"], kms_key) == b"piped value"
    before = project.read_bytes()
    failed(project.run("set", "MISSING_FILE", "--from-file", "absent.txt", "-p", "kms"))
    assert project.read_bytes() == before


def test_set_metadata_fields(project, kms_key):
    project.write("fnox.toml", providers(kms_key) + SENTINEL)
    ok(project.run("set", "DESCRIBED", "v1", "-p", "kms", "-d", "My KMS secret", "--default", "fallback",
                   "--if-missing", "ignore"))
    entry = project.config()["secrets"]["DESCRIBED"]
    assert {k: v for k, v in entry.items() if k != "value"} == {
        "provider": "kms", "description": "My KMS secret", "default": "fallback", "if_missing": "ignore"}
    assert kms_plaintext(entry["value"], kms_key) == b"v1"
    # Metadata only (empty stdin): the stored ciphertext is kept.
    ok(project.run("set", "DESCRIBED", "-d", "changed description", stdin=""))
    after = project.config()["secrets"]["DESCRIBED"]
    assert after == {**entry, "description": "changed description"}
    # A brand-new key with only metadata records no value.
    ok(project.run("set", "META_ONLY", "-p", "kms", "-d", "meta only", stdin=""))
    assert project.config()["secrets"]["META_ONLY"] == {"provider": "kms", "description": "meta only"}


def test_set_dry_run_changes_nothing(project, kms_key, ps_prefix):
    project.write("fnox.toml", providers(kms_key, ps_prefix) + SENTINEL)
    before = project.read_bytes()
    ok(project.run("set", "-n", "DRY_KMS", "value", "-p", "kms"))
    ok(project.run("set", "--dry-run", "DRY_PS", "value", "-p", "ps"))
    assert project.read_bytes() == before
    assert parameter(ps_prefix + "DRY_PS") is None


def test_set_rejects_invalid_name_and_unknown_provider(project, kms_key):
    project.write("fnox.toml", providers(kms_key) + SENTINEL)
    before = project.read_bytes()
    failed(project.run("set", "BAD-NAME", "x", "-p", "kms"))
    failed(project.run("set", "9STARTS_WITH_DIGIT", "x", "-p", "kms"))
    failed(project.run("set", "GOOD_NAME", "x", "-p", "nosuch"))
    assert project.read_bytes() == before


def test_provider_selection_without_flag(project, kms_keys, ps_prefix):
    k1, k2 = kms_keys()["KeyId"], kms_keys()["KeyId"]
    # Exactly one provider: it is used.
    project.write("fnox.toml", providers(k1) + SENTINEL)
    ok(project.run("set", "ONLY_ONE", "single"))
    entry = project.config()["secrets"]["ONLY_ONE"]
    assert entry["provider"] == "kms" and kms_plaintext(entry["value"], k1) == b"single"
    # default_provider picks among several.
    project.write("fnox.toml", 'default_provider = "second"\n' + providers(k1, ps_prefix) +
                  f'[providers.second]\ntype = "aws-kms"\nkey_id = "{k2}"\nregion = "us-east-1"\n\n' + SENTINEL)
    ok(project.run("set", "DEFAULTED", "to-second"))
    entry = project.config()["secrets"]["DEFAULTED"]
    assert entry["provider"] == "second" and kms_plaintext(entry["value"], k2) == b"to-second"
    # Several providers and no default: the value is kept in the config as plaintext.
    project.write("fnox.toml", providers(k1, ps_prefix) + SENTINEL)
    ok(project.run("set", "UNROUTED", "plain-v"))
    entry = project.config()["secrets"]["UNROUTED"]
    assert entry == {"value": "plain-v", "default": "plain-v"}
    assert parameter(ps_prefix + "UNROUTED") is None
    assert ok(project.run("get", "UNROUTED")).stdout == "plain-v\n"


def test_set_generate(project, kms_key):
    project.write("fnox.toml", providers(kms_key) + SENTINEL)
    result = ok(project.run("set", "GEN", "--generate", "--length", "24", "-p", "kms"))
    generated = kms_plaintext(project.config()["secrets"]["GEN"]["value"], kms_key).decode()
    assert len(generated) == 24 and generated not in result.stdout
    shown = ok(project.run("set", "HEX", "--generate", "--generate-type", "hex", "--length", "16", "--show", "-p", "kms"))
    hexed = kms_plaintext(project.config()["secrets"]["HEX"]["value"], kms_key).decode()
    assert len(hexed) == 16 and set(hexed) <= set("0123456789abcdef") and hexed in shown.stdout
    ok(project.run("set", "UUID", "--generate", "--generate-type", "uuid", "-p", "kms"))
    uuid_value = kms_plaintext(project.config()["secrets"]["UUID"]["value"], kms_key).decode()
    assert str(__import__("uuid").UUID(uuid_value)) == uuid_value.lower()
    ok(project.run("set", "DEFAULT_LEN", "--generate", "-p", "kms"))
    assert len(kms_plaintext(project.config()["secrets"]["DEFAULT_LEN"]["value"], kms_key)) == 32
    # An existing secret is only replaced with --force.
    before = project.read_bytes()
    failed(project.run("set", "GEN", "--generate"))
    failed(project.run("set", "B64", "--generate", "--generate-type", "base64", "--length", "13", "-p", "kms"))
    assert project.read_bytes() == before
    ok(project.run("set", "GEN", "--generate", "--force"))
    replaced = kms_plaintext(project.config()["secrets"]["GEN"]["value"], kms_key).decode()
    assert replaced != generated and len(replaced) == 32
    assert ok(project.run("get", "GEN")).stdout == replaced + "\n"


# --- aws-ps --------------------------------------------------------------------------------------

@pytest.mark.parametrize("endpoint", [False, True], ids=["env-endpoint", "config-endpoint"])
def test_ps_set_writes_secure_string(project, ps_prefix, endpoint):
    project.write("fnox.toml", providers(ps=ps_prefix, endpoint=endpoint) + SENTINEL)
    ok(project.run("set", "DB_URL", "postgres://u:p@h/db", "--provider", "ps"))
    assert parameter(ps_prefix + "DB_URL") == ("SecureString", "postgres://u:p@h/db")
    secrets = project.config()["secrets"]
    assert secrets["DB_URL"] == {"provider": "ps", "value": "DB_URL"}
    assert secrets["KEEP"] == {"default": "keep-me", "description": "unrelated"}
    assert "postgres://" not in project.read_text()
    assert ok(project.run("get", "DB_URL")).stdout == "postgres://u:p@h/db\n"


def test_ps_key_name_and_update_overwrite(project, ps_prefix):
    project.write("fnox.toml", providers(ps=ps_prefix) + SENTINEL)
    ok(project.run("set", "PSK", "first", "--provider", "ps", "-k", "custom/name"))
    assert project.config()["secrets"]["PSK"] == {"provider": "ps", "value": "custom/name"}
    assert parameter(ps_prefix + "custom/name") == ("SecureString", "first")
    assert parameter(ps_prefix + "PSK") is None
    # Updating keeps the recorded remote name and overwrites the parameter.
    ok(project.run("set", "PSK", "second"))
    assert project.config()["secrets"]["PSK"] == {"provider": "ps", "value": "custom/name"}
    assert parameter(ps_prefix + "custom/name") == ("SecureString", "second")
    assert parameter(ps_prefix + "PSK") is None
    versions = boto3.client("ssm").get_parameter(Name=ps_prefix + "custom/name")["Parameter"]["Version"]
    assert versions == 2
    assert ok(project.run("get", "PSK")).stdout == "second\n"


def test_ps_get_prefix_rules(project, ps_prefix):
    ssm = boto3.client("ssm")
    ssm.put_parameter(Name=ps_prefix + "app/prod/database/url", Value="postgres://localhost/mydb", Type="SecureString")
    ssm.put_parameter(Name=ps_prefix + "plain", Value="plain-string", Type="String")
    special = 'p@ssw0rd!#$%^&*()_+-={}[]|\\:";\'<>?,./~`'
    ssm.put_parameter(Name=ps_prefix + "special", Value=special, Type="SecureString")
    ssm.put_parameter(Name=ps_prefix + "multi", Value="line1\nline2", Type="SecureString")
    project.write("fnox.toml", providers(ps=ps_prefix + "app/prod/") +
                  f'[providers.raw]\ntype = "aws-ps"\nregion = "us-east-1"\n\n'
                  '[secrets]\nDATABASE_URL = { provider = "ps", value = "database/url" }\n'
                  f'FULL = {{ provider = "raw", value = "{ps_prefix}plain" }}\n'
                  f'SPECIAL = {{ provider = "raw", value = "{ps_prefix}special" }}\n'
                  f'MULTI = {{ provider = "raw", value = "{ps_prefix}multi" }}\n'
                  'ABSENT = { provider = "ps", value = "does-not-exist" }\n')
    assert ok(project.run("get", "DATABASE_URL")).stdout == "postgres://localhost/mydb\n"
    assert ok(project.run("get", "FULL")).stdout == "plain-string\n"
    assert ok(project.run("get", "SPECIAL")).stdout == special + "\n"
    assert ok(project.run("get", "MULTI")).stdout == "line1\nline2\n"
    assert failed(project.run("get", "ABSENT")).stdout == ""


# --- aws-sm --------------------------------------------------------------------------------------

def test_sm_set_updates_existing_secret(project, sm_prefix):
    sm = boto3.client("secretsmanager")
    sm.create_secret(Name=sm_prefix + "SM_TOKEN", SecretString="old-value")
    sm.create_secret(Name=sm_prefix + "remote/other", SecretString="old-other")
    project.write("fnox.toml", providers(sm=sm_prefix) + SENTINEL)
    ok(project.run("set", "SM_TOKEN", "sm-secret-value", "--provider", "sm"))
    ok(project.run("set", "RENAMED", "renamed-value", "--provider", "sm", "--key-name", "remote/other"))
    assert secret_string(sm_prefix + "SM_TOKEN") == "sm-secret-value"
    assert secret_string(sm_prefix + "remote/other") == "renamed-value"
    secrets = project.config()["secrets"]
    assert secrets["SM_TOKEN"] == {"provider": "sm", "value": "SM_TOKEN"}
    assert secrets["RENAMED"] == {"provider": "sm", "value": "remote/other"}
    assert ok(project.run("get", "SM_TOKEN")).stdout == "sm-secret-value\n"


def test_sm_set_on_missing_secret_is_refused(project, sm_prefix):
    project.write("fnox.toml", providers(sm=sm_prefix) + SENTINEL)
    before = project.read_bytes()
    failed(project.run("set", "SM_NEW", "never-stored", "--provider", "sm"))
    failed(project.run("set", "SM_NEW2", "never-stored", "--provider", "sm", "-k", "elsewhere"))
    assert project.read_bytes() == before
    assert secret_string(sm_prefix + "SM_NEW") is None
    assert secret_string(sm_prefix + "elsewhere") is None


def test_sm_get_json_path_and_line(project, sm_prefix):
    sm = boto3.client("secretsmanager")
    sm.create_secret(Name=sm_prefix + "json", SecretString=json.dumps(
        {"user": "admin", "password": "hunter2", "a.b": "dotted", "nested": {"k": "deep"}, "num": 5, "flag": True}))
    sm.create_secret(Name=sm_prefix + "raw", SecretString="raw secret")
    sm.create_secret(Name=sm_prefix + "lines", SecretString="l1\nl2\nl3")
    project.write("fnox.toml", providers(sm=sm_prefix) + """\
[secrets]
USER = { provider = "sm", value = "json", json_path = "user" }
PASSWORD = { provider = "sm", value = "json", json_path = "password" }
DOTTED = { provider = "sm", value = "json", json_path = "a\\\\.b" }
NESTED = { provider = "sm", value = "json", json_path = "nested.k" }
NUMBER = { provider = "sm", value = "json", json_path = "num" }
FLAG = { provider = "sm", value = "json", json_path = "flag" }
OBJECT = { provider = "sm", value = "json", json_path = "nested" }
WHOLE = { provider = "sm", value = "json" }
RAW = { provider = "sm", value = "raw" }
SECOND_LINE = { provider = "sm", value = "lines", line = 2 }
BAD_PATH = { provider = "sm", value = "json", json_path = "absent" }
GONE = { provider = "sm", value = "gone" }
""")
    expected = {"USER": "admin", "PASSWORD": "hunter2", "DOTTED": "dotted", "NESTED": "deep", "NUMBER": "5",
                "FLAG": "true", "RAW": "raw secret", "SECOND_LINE": "l2"}
    for name, value in expected.items():
        assert ok(project.run("get", name)).stdout == value + "\n", name
    assert json.loads(ok(project.run("get", "OBJECT")).stdout) == {"k": "deep"}
    assert json.loads(ok(project.run("get", "WHOLE")).stdout)["nested"] == {"k": "deep"}
    assert failed(project.run("get", "BAD_PATH")).stdout == ""
    assert failed(project.run("get", "GONE")).stdout == ""


# --- role_arn ------------------------------------------------------------------------------------

def test_role_arn_providers_assume_role(project, ps_prefix, sm_prefix, kms_key, iam_roles):
    _, arn = iam_roles()
    boto3.client("ssm").put_parameter(Name=ps_prefix + "R", Value="role-ps", Type="SecureString")
    boto3.client("secretsmanager").create_secret(Name=sm_prefix + "R", SecretString="role-sm")
    text = providers(kms_key, ps_prefix, sm_prefix).replace('region = "us-east-1"\n', f'region = "us-east-1"\nrole_arn = "{arn}"\n')
    project.write("fnox.toml", text + f'[secrets]\nRP = {{ provider = "ps", value = "R" }}\nRS = {{ provider = "sm", value = "R" }}\n'
                  f'RK = {{ provider = "kms", value = "{kms_ciphertext("role-kms", kms_key)}" }}\n')
    assert ok(project.run("get", "RP")).stdout == "role-ps\n"
    assert ok(project.run("get", "RS")).stdout == "role-sm\n"
    assert ok(project.run("get", "RK")).stdout == "role-kms\n"
    # Providers that assume a role do not leak session credentials into the child.
    script = 'printf "%s|%s|%s|%s" "$RP" "$RS" "$RK" "${AWS_SESSION_TOKEN-unset}"'
    assert ok(project.run("exec", "--", "bash", "-c", script)).stdout == "role-ps|role-sm|role-kms|unset"


def test_role_arn_must_be_an_iam_role_arn(project, ps_prefix, sm_prefix, iam_roles, uniq):
    boto3.client("ssm").put_parameter(Name=ps_prefix + "R", Value="role-ps", Type="SecureString")
    boto3.client("secretsmanager").create_secret(Name=sm_prefix + "R", SecretString="role-sm")
    for provider_type, prefix in (("aws-ps", ps_prefix), ("aws-sm", sm_prefix)):
        for arn in ("fnox-test-role", "arn:aws:iam:us-east-1:000000000000:role/x",
                    "arn:aws:iam::not-an-account:role/x", f"arn:aws:iam::000000000000:role/sem-{uniq}-never-created"):
            project.write("fnox.toml", f'[providers.p]\ntype = "{provider_type}"\nregion = "us-east-1"\nprefix = "{prefix}"\n'
                          f'role_arn = "{arn}"\n\n[secrets]\nS = {{ provider = "p", value = "R" }}\n')
            assert failed(project.run("get", "S")).stdout == "", (provider_type, arn)
