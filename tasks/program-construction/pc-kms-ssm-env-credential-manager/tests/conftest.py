"""Shared fixtures: an isolated project per test and per-test AWS resources with guaranteed teardown."""

import json
import uuid

import boto3
import pytest

from harness import workspace
from support import Project


@pytest.fixture
def project():
    with workspace() as root:
        yield Project(root)


@pytest.fixture
def uniq():
    return "t" + uuid.uuid4().hex[:12]


@pytest.fixture
def kms_keys():
    """Factory for fresh KMS keys; each key is scheduled for deletion afterwards."""
    kms = boto3.client("kms")
    created = []

    def make(alias=None):
        key = kms.create_key(Description="sealed-env-manager test")["KeyMetadata"]
        created.append(key["KeyId"])
        if alias:
            kms.create_alias(AliasName=alias, TargetKeyId=key["KeyId"])
            created.append(alias)
        return key

    yield make
    for key in created:
        try:
            if key.startswith("alias/"):
                kms.delete_alias(AliasName=key)
            else:
                kms.schedule_key_deletion(KeyId=key, PendingWindowInDays=7)
        except Exception:  # noqa: BLE001 -- cleanup must not mask the test result
            pass


@pytest.fixture
def kms_key(kms_keys):
    return kms_keys()["KeyId"]


@pytest.fixture
def ps_prefix(uniq):
    """A unique SSM name prefix; every parameter under it is deleted afterwards."""
    prefix = f"/sem-test/{uniq}/"
    yield prefix
    ssm = boto3.client("ssm")
    try:
        names = []
        for page in ssm.get_paginator("describe_parameters").paginate(
            ParameterFilters=[{"Key": "Name", "Option": "BeginsWith", "Values": [prefix]}]
        ):
            names += [p["Name"] for p in page["Parameters"]]
        for name in names:
            ssm.delete_parameter(Name=name)
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture
def sm_prefix(uniq):
    """A unique Secrets Manager name prefix; every secret under it is deleted afterwards."""
    prefix = f"sem-test/{uniq}/"
    yield prefix
    sm = boto3.client("secretsmanager")
    try:
        for page in sm.get_paginator("list_secrets").paginate():
            for entry in page["SecretList"]:
                if entry["Name"].startswith(prefix):
                    sm.delete_secret(SecretId=entry["Name"], ForceDeleteWithoutRecovery=True)
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture
def iam_roles(uniq):
    """Factory for assumable IAM roles (the venue checks that an assumed role exists)."""
    iam = boto3.client("iam")
    created = []
    trust = json.dumps({"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Principal": {"AWS": "*"}, "Action": "sts:AssumeRole"}]})

    def make(suffix="role"):
        name = f"sem-{uniq}-{suffix}"
        arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=trust)["Role"]["Arn"]
        created.append(name)
        return name, arn

    yield make
    for name in created:
        try:
            iam.delete_role(RoleName=name)
        except Exception:  # noqa: BLE001
            pass
