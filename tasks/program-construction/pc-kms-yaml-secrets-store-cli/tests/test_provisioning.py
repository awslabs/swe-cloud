"""`kms init` and `kms deprovision`: CloudFormation stack, alias and the file's `_keys` template."""

import boto3
import pytest
from conftest import alias_entry, content, failed, load, make_value, ok, run, stack, stack_resources, stored, template, write_doc

R = ["-r", "us-east-1"]
ROOT = "arn:aws:iam::000000000000:root"


def _parameters(label):
    return {p["ParameterKey"]: p["ParameterValue"] for p in stack(label).get("Parameters", [])}


def _key_of(label):
    alias = alias_entry(label)
    assert alias is not None, label
    return boto3.client("kms").describe_key(KeyId=alias["TargetKeyId"])["KeyMetadata"]


def _role_exists(name):
    iam = boto3.client("iam")
    try:
        iam.get_role(RoleName=name)
    except iam.exceptions.NoSuchEntityException:
        return False
    return True


def _assert_provisioned(label, *, simple_roles=False, iam_policies=True, admins=(ROOT,), users=(ROOT,)):
    found = stack(label)
    assert found is not None and found["StackStatus"] == "CREATE_COMPLETE", found
    parameters = _parameters(label)
    # Principal lists are compared without order but with multiplicity (duplicates are removed).
    principals = {name: sorted(parameters.pop(name, "").split(",")) for name in ("AdministratorPrincipals", "UserPrincipals")}
    assert principals == {"AdministratorPrincipals": sorted(admins), "UserPrincipals": sorted(users)}
    assert parameters == {
        "KeyDescription": f"Key used for securing secrets ({label}).",
        "CreateSimpleRoles": "true" if simple_roles else "false",
        "AllowIAMPoliciesToControlKeyAccess": "true" if iam_policies else "false",
    }
    key = _key_of(label)
    assert key["Enabled"] and key["Description"] == f"Key used for securing secrets ({label})."
    resources = stack_resources(label)
    assert [r for r in resources if r[0] == "AWS::KMS::Key"] == [("AWS::KMS::Key", key["KeyId"])]
    roles = sorted(physical for kind, physical in resources if kind == "AWS::IAM::Role")
    assert len(roles) == (2 if simple_roles else 0)
    assert len(resources) == 1 + len(roles)
    return key, roles


@pytest.mark.timeout(180)
def test_init_provisions_stack_alias_and_template(ws, labels):
    label = labels()
    ok(run(["kms", "init", *R, "-l", label, "-f", "s.yml"], ws, timeout=150))
    key, _ = _assert_provisioned(label)
    alias = alias_entry(label)
    assert alias["TargetKeyId"] == key["KeyId"]
    doc = load(ws / "s.yml")
    assert sorted(doc) == ["_keys"]
    assert template(doc) == [(alias["AliasArn"], "secretbox")]
    # The template then drives put; values record the key ARN behind the alias.
    ok(run(["put", "-f", "s.yml", "launch_codes", "0000"], ws))
    assert stored(load(ws / "s.yml"), "launch_codes") == [(key["Arn"], "secretbox", b"0000")]
    assert ok(run(["get", "-f", "s.yml", "launch_codes"], ws)).stdout == "0000"


@pytest.mark.timeout(180)
def test_init_flags_shape_stack_parameters_and_roles(ws, labels):
    label = labels()
    ok(run(["kms", "init", *R, "--label", label, "--filename", "s.yml", "--algorithm", "aesgcm256",
            "--create-simple-roles", "--disable-iam-policies",
            "-d", "jeff, role/web,user/bob,jeff", "-u", "arn:aws:iam::000000000000:user/zed,carol"], ws, timeout=150))
    _, roles = _assert_provisioned(
        label, simple_roles=True, iam_policies=False,
        admins=("arn:aws:iam::000000000000:role/web", ROOT,
                "arn:aws:iam::000000000000:user/bob", "arn:aws:iam::000000000000:user/jeff"),
        users=(ROOT, "arn:aws:iam::000000000000:user/carol", "arn:aws:iam::000000000000:user/zed"),
    )
    assert all(_role_exists(role) for role in roles)
    assert template(load(ws / "s.yml")) == [(alias_entry(label)["AliasArn"], "aesgcm256")]
    ok(run(["kms", "deprovision", *R, "-l", label, "--destructive"], ws, timeout=150))
    assert stack(label) is None and alias_entry(label) is None
    assert not any(_role_exists(role) for role in roles)


@pytest.mark.timeout(180)
def test_init_separate_flags_and_environment_defaults(ws, labels):
    label = labels()
    env = {"BISCUIT_LABEL": label, "BISCUIT_REGIONS": "us-east-1", "BISCUIT_FILENAME": "env.yml", "BISCUIT_ALGORITHM": "aesgcm256"}
    ok(run(["kms", "init", "--users", "role/reader"], ws, env=env, timeout=150))
    _assert_provisioned(label, users=(ROOT, "arn:aws:iam::000000000000:role/reader"))
    assert template(load(ws / "env.yml")) == [(alias_entry(label)["AliasArn"], "aesgcm256")]
    ok(run(["kms", "deprovision", "--destructive"], ws, env=env, timeout=150))
    assert stack(label) is None and alias_entry(label) is None


@pytest.mark.timeout(180)
def test_init_uses_existing_alias_without_stack(ws, keys, labels):
    label = labels()
    key = keys(alias=f"alias/biscuit-{label}")
    ok(run(["kms", "init", *R, "-l", label, "-f", "s.yml", "-a", "aesgcm256"], ws, timeout=150))
    assert stack(label) is None
    alias = alias_entry(label)
    assert alias["TargetKeyId"] == key["KeyId"]
    assert template(load(ws / "s.yml")) == [(alias["AliasArn"], "aesgcm256")]
    # With the single region already provisioned, --create-missing-keys has nothing to create.
    ok(run(["kms", "init", *R, "-l", label, "-f", "s.yml", "--create-missing-keys"], ws, timeout=150))
    assert stack(label) is None
    assert alias_entry(label) == alias
    assert template(load(ws / "s.yml")) == [(alias["AliasArn"], "secretbox")]
    # Deprovisioning removes only the alias; there is no stack, and the key itself stays.
    ok(run(["kms", "deprovision", *R, "-l", label, "--destructive"], ws, timeout=150))
    assert alias_entry(label) is None
    assert boto3.client("kms").describe_key(KeyId=key["KeyId"])["KeyMetadata"]["KeyState"] == "Enabled"
    assert template(load(ws / "s.yml")) == [(alias["AliasArn"], "secretbox")]


@pytest.mark.timeout(180)
def test_init_merges_into_existing_file(ws, keys, labels):
    label = labels()
    other = keys()
    secret = make_value(other["Arn"], "existing", "aesgcm256", b"still here")
    write_doc(ws / "s.yml", {
        "_keys": [{"key_id": other["Arn"], "key_manager": "kms", "algorithm": "aesgcm256"}],
        "existing": [secret],
    })
    ok(run(["kms", "init", *R, "-l", label, "-f", "s.yml"], ws, timeout=150))
    alias_arn = alias_entry(label)["AliasArn"]
    doc = load(ws / "s.yml")
    assert sorted(doc) == ["_keys", "existing"]
    assert doc["existing"] == [secret]
    assert template(doc) == sorted([(other["Arn"], "aesgcm256"), (alias_arn, "secretbox")])
    # Re-running for the same label replaces that entry's algorithm without duplicating it.
    ok(run(["kms", "init", *R, "-l", label, "-f", "s.yml", "-a", "aesgcm256"], ws, timeout=150))
    doc = load(ws / "s.yml")
    assert template(doc) == sorted([(other["Arn"], "aesgcm256"), (alias_arn, "aesgcm256")])
    assert doc["existing"] == [secret]
    _assert_provisioned(label)
    # put now encrypts under both template keys.
    ok(run(["put", "-f", "s.yml", "both", "two keys"], ws))
    key = _key_of(label)
    assert stored(load(ws / "s.yml"), "both") == sorted([(other["Arn"], "aesgcm256", b"two keys"), (key["Arn"], "aesgcm256", b"two keys")])


@pytest.mark.timeout(180)
def test_init_adds_template_to_file_without_one(ws, labels):
    # A file without `_keys` gains one; its secrets are kept.
    label = labels()
    write_doc(ws / "s.yml", {"orphan": [{"key_id": "x", "key_manager": "kms", "algorithm": "secretbox", "ciphertext": "AA=="}]})
    ok(run(["kms", "init", *R, "-l", label, "-f", "s.yml"], ws, timeout=150))
    doc = load(ws / "s.yml")
    assert sorted(doc) == ["_keys", "orphan"]
    assert doc["orphan"] == [{"key_id": "x", "key_manager": "kms", "algorithm": "secretbox", "ciphertext": "AA=="}]
    assert template(doc) == [(alias_entry(label)["AliasArn"], "secretbox")]


@pytest.mark.timeout(180)
def test_init_refuses_stack_without_alias(ws, labels):
    label = labels()
    ok(run(["kms", "init", *R, "-l", label, "-f", "first.yml"], ws, timeout=150))
    alias = alias_entry(label)
    boto3.client("kms").delete_alias(AliasName=alias["AliasName"])
    first = content(ws / "first.yml")
    failed(run(["kms", "init", *R, "-l", label, "-f", "first.yml"], ws, timeout=150))
    failed(run(["kms", "init", *R, "-l", label, "-f", "fresh.yml"], ws, timeout=150))
    assert content(ws / "first.yml") == first
    assert not (ws / "fresh.yml").exists()
    assert alias_entry(label) is None
    assert stack(label)["StackStatus"] == "CREATE_COMPLETE"
    # deprovision still removes the orphaned stack.
    ok(run(["kms", "deprovision", *R, "-l", label, "--destructive"], ws, timeout=150))
    assert stack(label) is None


@pytest.mark.timeout(180)
def test_init_refuses_disabled_key(ws, keys, labels):
    label = labels()
    key = keys(alias=f"alias/biscuit-{label}")
    boto3.client("kms").disable_key(KeyId=key["KeyId"])
    failed(run(["kms", "init", *R, "-l", label, "-f", "s.yml"], ws, timeout=150))
    assert not (ws / "s.yml").exists()
    assert stack(label) is None
    assert alias_entry(label)["TargetKeyId"] == key["KeyId"]


@pytest.mark.timeout(180)
def test_label_validation_and_trimming(ws, labels):
    label = labels()
    for bad in ("bad label", "a" * 21, "", "dot.ted", "slash/x"):
        failed(run(["kms", "init", *R, "-l", bad, "-f", "s.yml"], ws))
        failed(run(["kms", "deprovision", *R, "-l", bad, "--destructive"], ws))
    assert list(ws.iterdir()) == []
    ok(run(["kms", "init", *R, "-l", f"  {label}  ", "-f", "s.yml"], ws, timeout=150))
    _assert_provisioned(label)
    assert template(load(ws / "s.yml")) == [(alias_entry(label)["AliasArn"], "secretbox")]


@pytest.mark.timeout(180)
def test_twenty_character_label_is_accepted(ws, labels):
    label = labels(size=20)
    ok(run(["kms", "init", *R, "-l", label, "-f", "s.yml"], ws, timeout=150))
    _assert_provisioned(label)
    ok(run(["kms", "deprovision", *R, "-l", label, "--destructive"], ws, timeout=150))
    assert stack(label) is None and alias_entry(label) is None


@pytest.mark.timeout(180)
def test_deprovision_without_destructive_changes_nothing(ws, labels):
    label = labels()
    ok(run(["kms", "init", *R, "-l", label, "-f", "s.yml"], ws, timeout=150))
    alias = alias_entry(label)
    before = content(ws / "s.yml")
    ok(run(["kms", "deprovision", *R, "-l", label], ws, timeout=150))
    assert alias_entry(label) == alias
    _assert_provisioned(label)
    assert content(ws / "s.yml") == before
    ok(run(["kms", "deprovision", *R, "-l", label, "--destructive"], ws, timeout=150))
    assert stack(label) is None and alias_entry(label) is None
    assert content(ws / "s.yml") == before


def test_deprovision_of_unknown_label_succeeds(ws, labels):
    label = labels()
    ok(run(["kms", "deprovision", *R, "-l", label], ws))
    ok(run(["kms", "deprovision", *R, "-l", label, "--destructive"], ws))
    assert stack(label) is None and alias_entry(label) is None
    assert list(ws.iterdir()) == []


def test_deprovision_leaves_other_labels(ws, keys, labels):
    mine, other = labels(), labels()
    keys(alias=f"alias/biscuit-{mine}")
    other_key = keys(alias=f"alias/biscuit-{other}")
    ok(run(["kms", "deprovision", *R, "-l", mine, "--destructive"], ws))
    assert alias_entry(mine) is None
    assert alias_entry(other)["TargetKeyId"] == other_key["KeyId"]
