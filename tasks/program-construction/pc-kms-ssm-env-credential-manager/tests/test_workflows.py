"""Multi-step workflows: import, reencrypt, remove, moving secrets between keys, and full lifecycles."""

import json

import boto3

from support import child_env, failed, kms_ciphertext, kms_plaintext, ok, parameter, secret_string

DOTENV = """\
# comment line
export EXP_A=alpha

SQ='single $HOME quoted'
DQ="double \\"esc\\" \\n newline"
UNQ=plain value  # inline comment
EQ=a=b=c
lower_ok=yes
URL=postgresql://localhost:5432/mydb
"""

IMPORTED = {"EXP_A": "alpha", "SQ": "single $HOME quoted", "DQ": 'double "esc" \n newline', "UNQ": "plain value",
            "EQ": "a=b=c", "lower_ok": "yes", "URL": "postgresql://localhost:5432/mydb"}


def kms_config(key, extra_providers="", secrets=""):
    return f"""\
root = true
# Main configuration file
[providers.kms]
type = "aws-kms"
key_id = "{key}"
region = "us-east-1"
{extra_providers}
# Application secrets
[secrets]
# Database connection string
KEEP = {{ default = "keep-me" }}
{secrets}# End of secrets section
"""


def decrypted_entries(project, keys, name="fnox.toml"):
    """{name: plaintext} for every provider-backed inline secret, using the provider -> key map."""
    out = {}
    for secret, entry in project.config(name)["secrets"].items():
        if "provider" in entry:
            assert set(entry) == {"provider", "value"}, entry
            out[secret] = kms_plaintext(entry["value"], keys[entry["provider"]]).decode()
    return out


def test_import_dotenv_encrypts_each_pair(project, kms_key):
    project.write("fnox.toml", kms_config(kms_key))
    project.write("in.env", DOTENV)
    ok(project.run("import", "-i", "in.env", "--provider", "kms", "--force", "env"))
    assert decrypted_entries(project, {"kms": kms_key}) == IMPORTED
    text = project.read_text()
    for plaintext in ("alpha", "postgresql://", "single $HOME"):
        assert plaintext not in text
    for comment in ("# Main configuration file", "# Application secrets", "# Database connection string"):
        assert comment in text
    assert project.config()["secrets"]["KEEP"] == {"default": "keep-me"}
    for name, value in IMPORTED.items():
        assert ok(project.run("get", name)).stdout == value + "\n"


def test_import_filter_prefix_and_formats(project, kms_key):
    project.write("fnox.toml", kms_config(kms_key))
    project.write("in.env", DOTENV)
    ok(project.run("import", "-i", "in.env", "-p", "kms", "-f", "--filter", "^(SQ|EQ)$", "--prefix", "P_", "env"))
    assert decrypted_entries(project, {"kms": kms_key}) == {"P_SQ": IMPORTED["SQ"], "P_EQ": "a=b=c"}
    project.write("in.json", json.dumps({"J1": "one", "J2": "two words"}))
    ok(project.run("import", "-i", "in.json", "-p", "kms", "-f", "json"))
    project.write("in.toml", 'T1 = "tone"\n')
    ok(project.run("import", "-i", "in.toml", "-p", "kms", "-f", "toml"))
    project.write("in.yaml", 'Y1: one\nY2: "two"\n')
    ok(project.run("import", "-i", "in.yaml", "-p", "kms", "-f", "yaml"))
    ok(project.run("import", "-p", "kms", "-f", "env", stdin="S1=from-stdin\n"))
    assert decrypted_entries(project, {"kms": kms_key}) == {
        "P_SQ": IMPORTED["SQ"], "P_EQ": "a=b=c", "J1": "one", "J2": "two words", "T1": "tone", "Y1": "one", "Y2": "two",
        "S1": "from-stdin"}


def test_import_refusals_leave_config_unchanged(project, kms_key, ps_prefix):
    project.write("fnox.toml", kms_config(kms_key, f'[providers.ps]\ntype = "aws-ps"\nregion = "us-east-1"\nprefix = "{ps_prefix}"\n'))
    project.write("in.env", DOTENV)
    before = project.read_bytes()
    # No --force and no terminal: nothing is imported.
    project.run("import", "-i", "in.env", "-p", "kms", "env")
    assert project.read_bytes() == before
    ok(project.run("import", "-i", "in.env", "-p", "kms", "--force", "--dry-run", "env"))
    assert project.read_bytes() == before
    failed(project.run("import", "-i", "in.env", "-p", "ps", "--force", "env"))
    assert parameter(ps_prefix + "EXP_A") is None
    failed(project.run("import", "-i", "absent.env", "-p", "kms", "--force", "env"))
    failed(project.run("import", "-i", "in.env", "-p", "nosuch", "--force", "env"))
    project.write("bad.env", "GOOD=1\nBAD-NAME=x\n")
    failed(project.run("import", "-i", "bad.env", "-p", "kms", "--force", "env"))
    # KMS refuses an empty plaintext; the whole import is abandoned.
    project.write("empty.env", "FIRST=ok\nEMPTYV=\nLAST=ok\n")
    failed(project.run("import", "-i", "empty.env", "-p", "kms", "--force", "env"))
    assert project.read_bytes() == before


def three_secrets(project, kms_keys):
    k1, k2 = kms_keys()["KeyId"], kms_keys()["KeyId"]
    project.write("fnox.toml", kms_config(k1, f'\n[providers.kms2]\ntype = "aws-kms"\nkey_id = "{k2}"\nregion = "us-east-1"\n',
                                         f'A = {{ provider = "kms", value = "{kms_ciphertext("alpha", k1)}" }}\n'
                                         f'B = {{ provider = "kms", value = "{kms_ciphertext("beta", k1)}" }}\n'
                                         f'C = {{ provider = "kms2", value = "{kms_ciphertext("gamma", k2)}" }}\n'))
    return k1, k2


def values_of(project):
    return {k: v.get("value") for k, v in project.config()["secrets"].items()}


def test_reencrypt_selection(project, kms_keys):
    k1, k2 = three_secrets(project, kms_keys)
    keys = {"kms": k1, "kms2": k2}
    original = values_of(project)
    ok(project.run("reencrypt", "--force"))
    after_all = values_of(project)
    assert all(after_all[n] != original[n] for n in "ABC") and after_all["KEEP"] is None
    assert decrypted_entries(project, keys) == {"A": "alpha", "B": "beta", "C": "gamma"}
    assert "# Database connection string" in project.read_text()
    ok(project.run("reencrypt", "--force", "-p", "kms2"))
    after_provider = values_of(project)
    assert [n for n in "ABC" if after_provider[n] != after_all[n]] == ["C"]
    ok(project.run("reencrypt", "--force", "--filter", "^B$"))
    after_filter = values_of(project)
    assert [n for n in "ABC" if after_filter[n] != after_provider[n]] == ["B"]
    ok(project.run("reencrypt", "--force", "A"))
    after_key = values_of(project)
    assert [n for n in "ABC" if after_key[n] != after_filter[n]] == ["A"]
    assert decrypted_entries(project, keys) == {"A": "alpha", "B": "beta", "C": "gamma"}
    before = project.read_bytes()
    ok(project.run("reencrypt", "--dry-run", "--force"))
    project.run("reencrypt")  # prompts; without a terminal nothing changes
    assert project.read_bytes() == before


def test_reencrypt_after_key_change_fails_atomically(project, kms_keys):
    k1, k2 = three_secrets(project, kms_keys)
    project.write("fnox.toml", project.read_text().replace(f'key_id = "{k1}"', f'key_id = "{k2}"', 1))
    before = project.read_bytes()
    failed(project.run("reencrypt", "--force"))
    assert project.read_bytes() == before
    failed(project.run("get", "A"))
    assert ok(project.run("get", "C")).stdout == "gamma\n"


def test_move_secret_between_keys(project, kms_keys):
    k1, k2 = three_secrets(project, kms_keys)
    moved = ok(project.run("get", "A")).stdout.rstrip("\n")
    ok(project.run("set", "A", "--provider", "kms2", stdin=moved))
    entry = project.config()["secrets"]["A"]
    assert entry["provider"] == "kms2" and kms_plaintext(entry["value"], k2) == b"alpha"
    assert decrypted_entries(project, {"kms": k1, "kms2": k2}) == {"A": "alpha", "B": "beta", "C": "gamma"}
    # The moved secret survives the old key's provider being repointed.
    project.write("fnox.toml", project.read_text().replace(f'key_id = "{k1}"', f'key_id = "{k2}"', 1))
    assert ok(project.run("get", "A")).stdout == "alpha\n"


def test_remove_keeps_comments_remote_values_and_other_entries(project, kms_key, ps_prefix, sm_prefix):
    boto3.client("secretsmanager").create_secret(Name=sm_prefix + "S", SecretString="old")
    project.write("fnox.toml", kms_config(kms_key, f'\n[providers.ps]\ntype = "aws-ps"\nregion = "us-east-1"\nprefix = "{ps_prefix}"\n'
                                                   f'\n[providers.sm]\ntype = "aws-sm"\nregion = "us-east-1"\nprefix = "{sm_prefix}"\n'))
    ok(project.run("set", "P", "ps-value", "-p", "ps"))
    ok(project.run("set", "S", "sm-value", "-p", "sm"))
    ok(project.run("set", "K", "kms-value", "-p", "kms"))
    before = project.read_bytes()
    ok(project.run("remove", "-n", "P"))
    assert project.read_bytes() == before
    ok(project.run("remove", "P"))
    ok(project.run("remove", "S"))
    cfg = project.config()
    assert set(cfg["secrets"]) == {"KEEP", "K"}
    assert set(cfg["providers"]) == {"kms", "ps", "sm"}
    text = project.read_text()
    for comment in ("# Main configuration file", "# Application secrets", "# End of secrets section"):
        assert comment in text
    assert parameter(ps_prefix + "P") == ("SecureString", "ps-value")
    assert secret_string(sm_prefix + "S") == "sm-value"
    assert ok(project.run("get", "K")).stdout == "kms-value\n"
    failed(project.run("get", "P"))
    before = project.read_bytes()
    failed(project.run("remove", "NOT_THERE"))
    assert project.read_bytes() == before
    ok(project.run("remove", "K"))
    ok(project.run("remove", "KEEP"))
    assert project.config().get("secrets", {}) == {}
    assert "# Main configuration file" in project.read_text()


def test_lifecycle_set_exec_update_remove(project, kms_key, ps_prefix, sm_prefix):
    boto3.client("secretsmanager").create_secret(Name=sm_prefix + "api", SecretString=json.dumps({"token": "t0"}))
    project.write("fnox.toml", kms_config(kms_key, f'\n[providers.ps]\ntype = "aws-ps"\nregion = "us-east-1"\nprefix = "{ps_prefix}"\n'
                                                   f'\n[providers.sm]\ntype = "aws-sm"\nregion = "us-east-1"\nprefix = "{sm_prefix}"\n',
                                         'TOKEN = { provider = "sm", value = "api", json_path = "token" }\n'))
    ok(project.run("set", "DB_PASS", "pw1", "-p", "kms"))
    ok(project.run("set", "DB_HOST", "db.internal", "-p", "ps"))
    names = ["DB_PASS", "DB_HOST", "TOKEN", "KEEP"]
    values, _ = child_env(project, names=names)
    assert values == {"DB_PASS": "pw1", "DB_HOST": "db.internal", "TOKEN": "t0", "KEEP": "keep-me"}
    # Updates go to the place each secret already lives.
    ok(project.run("set", "DB_PASS", "pw2"))
    ok(project.run("set", "DB_HOST", "db2.internal"))
    boto3.client("secretsmanager").put_secret_value(SecretId=sm_prefix + "api", SecretString=json.dumps({"token": "t1"}))
    assert parameter(ps_prefix + "DB_HOST") == ("SecureString", "db2.internal")
    assert kms_plaintext(project.config()["secrets"]["DB_PASS"]["value"], kms_key) == b"pw2"
    values, _ = child_env(project, names=names)
    assert values == {"DB_PASS": "pw2", "DB_HOST": "db2.internal", "TOKEN": "t1", "KEEP": "keep-me"}
    ok(project.run("remove", "DB_HOST"))
    values, _ = child_env(project, names=names)
    assert values == {"DB_PASS": "pw2", "TOKEN": "t1", "KEEP": "keep-me"}
    doc = json.loads(ok(project.run("export", "-f", "json")).stdout)
    assert doc["secrets"] == {"KEEP": "keep-me", "TOKEN": "t1", "DB_PASS": "pw2"}
