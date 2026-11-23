"""Profiles: provider overrides, inheritance, profile files, and which file a write lands in."""

import json

import boto3
import pytest

from support import child_env, failed, kms_plaintext, ok


@pytest.fixture
def layered(project, kms_keys, ps_prefix):
    k1, k2 = kms_keys()["KeyId"], kms_keys()["KeyId"]
    ssm = boto3.client("ssm")
    for name, value in (("dev/DB", "dev-db"), ("stg/DB", "stg-db"), ("prod/DB", "prod-db")):
        ssm.put_parameter(Name=ps_prefix + name, Value=value, Type="SecureString")
    project.write("fnox.toml", f"""\
root = true
default_provider = "kms"

[providers.kms]
type = "aws-kms"
key_id = "{k1}"
region = "us-east-1"

[providers.ps]
type = "aws-ps"
region = "us-east-1"
prefix = "{ps_prefix}dev/"

[secrets]
DB = {{ provider = "ps", value = "DB" }}
LEVEL = {{ default = "info" }}
COMMON = {{ default = "common" }}

# staging overrides
[profiles.staging]
default_provider = "kms2"

[profiles.staging.providers.ps]
type = "aws-ps"
region = "us-east-1"
prefix = "{ps_prefix}stg/"

[profiles.staging.providers.kms2]
type = "aws-kms"
key_id = "{k2}"
region = "us-east-1"

[profiles.staging.secrets]
LEVEL = {{ default = "debug" }}
STAGE_ONLY = {{ default = "stage" }}

[profiles.prod]
inherits = ["staging"]

[profiles.prod.providers.ps]
type = "aws-ps"
region = "us-east-1"
prefix = "{ps_prefix}prod/"

[profiles.prod.secrets]
PROD_ONLY = {{ default = "p" }}
""")
    return k1, k2


def exported(project, *args, env=None):
    return json.loads(ok(project.run(*args, "export", "-f", "json", env=env)).stdout)


def test_profile_overrides_providers_and_secrets(project, layered):
    assert exported(project)["secrets"] == {"DB": "dev-db", "LEVEL": "info", "COMMON": "common"}
    staging = exported(project, "-P", "staging")
    assert staging["secrets"] == {"DB": "stg-db", "LEVEL": "debug", "COMMON": "common", "STAGE_ONLY": "stage"}
    assert staging["metadata"]["profile"] == "staging"
    assert exported(project, env={"FNOX_PROFILE": "staging"})["secrets"] == staging["secrets"]
    assert exported(project, "-P", "staging", "--no-defaults")["secrets"] == {"LEVEL": "debug", "STAGE_ONLY": "stage"}
    failed(project.run("get", "STAGE_ONLY"))
    failed(project.run("-P", "nosuch", "get", "COMMON"))


def test_profile_inheritance(project, layered):
    prod = exported(project, "-P", "prod")["secrets"]
    assert prod == {"DB": "prod-db", "LEVEL": "debug", "COMMON": "common", "STAGE_ONLY": "stage", "PROD_ONLY": "p"}
    values, _ = child_env(project, "-P", "prod", names=["DB", "LEVEL", "PROD_ONLY", "STAGE_ONLY"])
    assert values == {"DB": "prod-db", "LEVEL": "debug", "PROD_ONLY": "p", "STAGE_ONLY": "stage"}


def test_profile_set_uses_profile_default_provider(project, layered):
    _, k2 = layered
    ok(project.run("-P", "staging", "set", "NEW_STAGE", "stg-secret"))
    cfg = project.config()
    assert "NEW_STAGE" not in cfg["secrets"]
    entry = cfg["profiles"]["staging"]["secrets"]["NEW_STAGE"]
    assert entry["provider"] == "kms2" and kms_plaintext(entry["value"], k2) == b"stg-secret"
    assert ok(project.run("-P", "staging", "get", "NEW_STAGE")).stdout == "stg-secret\n"
    failed(project.run("get", "NEW_STAGE"))
    assert "# staging overrides" in project.read_text()


def test_profile_set_to_remote_uses_profile_provider(project, layered, ps_prefix):
    ok(project.run("-P", "staging", "set", "REMOTE", "staged-remote", "-p", "ps"))
    assert project.config()["profiles"]["staging"]["secrets"]["REMOTE"] == {"provider": "ps", "value": "REMOTE"}
    got = boto3.client("ssm").get_parameter(Name=ps_prefix + "stg/REMOTE", WithDecryption=True)["Parameter"]
    assert (got["Type"], got["Value"]) == ("SecureString", "staged-remote")
    with pytest.raises(boto3.client("ssm").exceptions.ParameterNotFound):
        boto3.client("ssm").get_parameter(Name=ps_prefix + "dev/REMOTE")


def test_profile_remove_only_touches_profile(project, layered):
    ok(project.run("-P", "staging", "remove", "LEVEL"))
    cfg = project.config()
    assert cfg["secrets"]["LEVEL"] == {"default": "info"}
    assert set(cfg["profiles"]["staging"]["secrets"]) == {"STAGE_ONLY"}
    assert ok(project.run("-P", "staging", "get", "LEVEL")).stdout == "info\n"


def test_profile_file_next_to_config(project, layered):
    k1, _ = layered
    project.write("fnox.qa.toml", '[secrets]\nQA_ONLY = { default = "qa" }\nLEVEL = { default = "qa-level" }\n')
    assert exported(project, "-P", "qa")["secrets"] == {"DB": "dev-db", "LEVEL": "qa-level", "COMMON": "common", "QA_ONLY": "qa"}
    main_before = project.read_bytes()
    ok(project.run("-P", "qa", "set", "QA_NEW", "q1"))
    assert project.read_bytes() == main_before
    qa = project.config("fnox.qa.toml")["secrets"]
    assert set(qa) == {"QA_ONLY", "LEVEL", "QA_NEW"}
    assert qa["QA_NEW"]["provider"] == "kms" and kms_plaintext(qa["QA_NEW"]["value"], k1) == b"q1"
    ok(project.run("-P", "qa", "remove", "QA_ONLY"))
    assert set(project.config("fnox.qa.toml")["secrets"]) == {"LEVEL", "QA_NEW"}
    assert project.read_bytes() == main_before


def test_nested_project_writes_to_nearest_config(project, layered):
    k1, _ = layered
    child = project.mkdir("service")
    project.write("fnox.toml", '[secrets]\nCHILD = { default = "child" }\n', base=child)
    parent_before = project.read_bytes()
    ok(project.run("set", "CHILD_SET", "cv", "-p", "kms", cwd=child))
    assert project.read_bytes() == parent_before
    entry = project.config(base=child)["secrets"]["CHILD_SET"]
    assert kms_plaintext(entry["value"], k1) == b"cv"
    doc = json.loads(ok(project.run("export", "-f", "json", cwd=child)).stdout)
    assert doc["secrets"] == {"DB": "dev-db", "LEVEL": "info", "COMMON": "common", "CHILD": "child", "CHILD_SET": "cv"}


def test_layered_profiles_need_write_profile(project, layered):
    k1, k2 = layered
    staging_only = exported(project, "-P", "prod,staging")
    assert staging_only["secrets"]["DB"] == "stg-db"
    before = project.read_bytes()
    failed(project.run("-P", "staging,prod", "set", "TARGETED", "t"))
    assert project.read_bytes() == before
    ok(project.run("-P", "staging,prod", "--write-profile", "prod", "set", "TARGETED", "t", "-p", "kms"))
    cfg = project.config()
    entry = cfg["profiles"]["prod"]["secrets"]["TARGETED"]
    assert entry["provider"] == "kms" and kms_plaintext(entry["value"], k1) == b"t"
    assert "TARGETED" not in cfg["profiles"]["staging"]["secrets"] and "TARGETED" not in cfg["secrets"]
    ok(project.run("-P", "staging,prod", "--write-profile", "staging", "remove", "STAGE_ONLY"))
    assert set(project.config()["profiles"]["staging"]["secrets"]) == {"LEVEL"}


def test_global_flag_writes_global_config(project, layered):
    k1, _ = layered
    before = project.read_bytes()
    ok(project.run("set", "-g", "GLOBAL_SECRET", "gval", "-p", "kms"))
    project.write("g.env", "GLOBAL_IMPORTED=gimp\n")
    ok(project.run("import", "-g", "-i", "g.env", "-p", "kms", "-f", "env"))
    assert project.read_bytes() == before
    global_cfg = project.config(".config/fnox/config.toml", base=project.home)["secrets"]
    assert set(global_cfg) == {"GLOBAL_SECRET", "GLOBAL_IMPORTED"}
    assert kms_plaintext(global_cfg["GLOBAL_SECRET"]["value"], k1) == b"gval"
    assert kms_plaintext(global_cfg["GLOBAL_IMPORTED"]["value"], k1) == b"gimp"
    assert ok(project.run("get", "GLOBAL_SECRET")).stdout == "gval\n"
    ok(project.run("remove", "-g", "GLOBAL_SECRET"))
    assert set(project.config(".config/fnox/config.toml", base=project.home)["secrets"]) == {"GLOBAL_IMPORTED"}
    assert project.read_bytes() == before


def test_import_into_active_profile(project, layered):
    _, k2 = layered
    project.write("in.env", "STG_A=one\nSTG_B=two words\n")
    ok(project.run("-P", "staging", "import", "-i", "in.env", "-p", "kms2", "--force", "env"))
    cfg = project.config()
    assert "STG_A" not in cfg["secrets"]
    staged = cfg["profiles"]["staging"]["secrets"]
    assert {k: kms_plaintext(staged[k]["value"], k2) for k in ("STG_A", "STG_B")} == {"STG_A": b"one", "STG_B": b"two words"}
    assert staged["STG_A"]["provider"] == "kms2"
    assert exported(project, "-P", "staging")["secrets"]["STG_B"] == "two words"
    failed(project.run("get", "STG_A"))
