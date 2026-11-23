"""Shared fixtures: per-test KMS keys, provisioning labels and secrets-file inspection."""

import base64
import os
import uuid

import boto3
import botocore.exceptions
import nacl.secret
import pytest
import yaml
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from harness import run_program, workspace, write_fixture

ENV = {"AWS_ENDPOINT": "http://engines:4566", "AWS_REGION": "us-east-1"}
VALUE_FIELDS = {"key_id", "key_manager", "algorithm", "key_ciphertext", "ciphertext"}
TEMPLATE_FIELDS = {"key_id", "key_manager", "algorithm"}


def run(args, cwd, *, env=None, **options):
    """Run the candidate with the venue endpoint variables the interface documents."""
    return run_program(args, cwd=cwd, env={**ENV, **(env or {})}, **options)


def ok(result):
    assert result.returncode == 0, f"exit {result.returncode}\nstdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    return result


def failed(result):
    assert result.returncode != 0, f"expected failure\nstdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    return result


def load(path):
    """Decoded secrets file; every name maps to a list of mappings with string fields."""
    doc = yaml.safe_load(path.read_text())
    assert isinstance(doc, dict), doc
    for name, values in doc.items():
        assert type(name) is str and isinstance(values, list), (name, values)
        for value in values:
            assert isinstance(value, dict) and all(type(v) is str for v in value.values()), (name, value)
    return doc


def values(entries):
    """A value list as a multiset: order is not significant, multiplicity is."""
    return sorted(sorted(entry.items()) for entry in entries)


def content(path):
    """Decoded file content with every value list normalized to a multiset."""
    return {name: values(entries) for name, entries in load(path).items()}


def write_doc(path, doc):
    write_fixture(path, yaml.safe_dump(doc))


def template(doc):
    """`_keys` entries as a multiset of (key_id, algorithm); each entry has exactly the template fields."""
    entries = doc["_keys"]
    for entry in entries:
        assert set(entry) == TEMPLATE_FIELDS and entry["key_manager"] == "kms", entry
    return sorted((e["key_id"], e["algorithm"]) for e in entries)


def decrypt_value(name, value):
    """Independently open one stored value: KMS Decrypt with the SecretName context, then the local cipher."""
    assert set(value) == VALUE_FIELDS, value
    assert value["key_manager"] == "kms", value
    data_key = boto3.client("kms").decrypt(
        CiphertextBlob=base64.b64decode(value["key_ciphertext"], validate=True),
        EncryptionContext={"SecretName": name},
    )["Plaintext"]
    assert len(data_key) == 32
    blob = base64.b64decode(value["ciphertext"], validate=True)
    if value["algorithm"] == "aesgcm256":
        return AESGCM(data_key).decrypt(blob[-12:], blob[:-12], None)
    assert value["algorithm"] == "secretbox", value
    return nacl.secret.SecretBox(data_key).decrypt(blob[24:], blob[:24])


def stored(doc, name):
    """Multiset of (key_id, algorithm, plaintext) for every stored value of a secret."""
    return sorted((v["key_id"], v["algorithm"], decrypt_value(name, v)) for v in doc[name])


def make_value(key_arn, name, algorithm, plaintext, *, context=None):
    """Build a stored value in the documented public layout, as an input fixture."""
    data_key = boto3.client("kms").generate_data_key(
        KeyId=key_arn, NumberOfBytes=32, EncryptionContext={"SecretName": context or name}
    )
    if algorithm == "aesgcm256":
        nonce = os.urandom(12)
        blob = AESGCM(data_key["Plaintext"]).encrypt(nonce, plaintext, None) + nonce
    else:
        nonce = os.urandom(24)
        blob = nonce + nacl.secret.SecretBox(data_key["Plaintext"]).encrypt(plaintext, nonce).ciphertext
    return {
        "key_id": key_arn,
        "key_manager": "kms",
        "algorithm": algorithm,
        "key_ciphertext": base64.b64encode(data_key["CiphertextBlob"]).decode(),
        "ciphertext": base64.b64encode(blob).decode(),
    }


def opens_under(name, value):
    """Whether a stored value decrypts when bound to `name`."""
    try:
        decrypt_value(name, value)
    except botocore.exceptions.ClientError as error:
        assert error.response["Error"]["Code"] == "InvalidCiphertextException", error
        return False
    return True


@pytest.fixture
def ws():
    with workspace() as root:
        yield root


@pytest.fixture
def keys():
    """Factory of fresh KMS keys (optionally aliased); all are cleaned up after the test."""
    kms = boto3.client("kms")
    created, aliases = [], []

    def make(alias=None):
        meta = kms.create_key(Description="reconstruction test key")["KeyMetadata"]
        created.append(meta["KeyId"])
        if alias:
            kms.create_alias(AliasName=alias, TargetKeyId=meta["KeyId"])
            aliases.append(alias)
        return meta

    yield make
    for alias in aliases:
        try:
            kms.delete_alias(AliasName=alias)
        except Exception:
            pass
    for key in created:
        try:
            kms.schedule_key_deletion(KeyId=key, PendingWindowInDays=7)
        except Exception:
            pass


def alias_entry(label):
    for page in boto3.client("kms").get_paginator("list_aliases").paginate():
        for alias in page["Aliases"]:
            if alias["AliasName"] == f"alias/biscuit-{label}":
                return alias
    return None


def stack(label):
    cfn = boto3.client("cloudformation")
    try:
        stacks = cfn.describe_stacks(StackName=f"biscuit-{label}")["Stacks"]
    except cfn.exceptions.ClientError as error:
        assert "does not exist" in str(error), error
        return None
    if not stacks or stacks[0]["StackStatus"] == "DELETE_COMPLETE":
        return None
    return stacks[0]


def stack_resources(label):
    summaries = boto3.client("cloudformation").list_stack_resources(StackName=f"biscuit-{label}")["StackResourceSummaries"]
    return sorted((r["ResourceType"], r["PhysicalResourceId"]) for r in summaries)


@pytest.fixture
def labels():
    """Factory of unique labels; teardown removes any alias/stack left for them."""
    made = []

    def make(size=13):
        label = "t" + uuid.uuid4().hex[: size - 1]
        made.append(label)
        return label

    yield make
    kms, cfn = boto3.client("kms"), boto3.client("cloudformation")
    for label in made:
        alias = alias_entry(label)
        if alias:
            try:
                kms.delete_alias(AliasName=alias["AliasName"])
                kms.schedule_key_deletion(KeyId=alias["TargetKeyId"], PendingWindowInDays=7)
            except Exception:
                pass
        try:
            if stack(label):
                cfn.delete_stack(StackName=f"biscuit-{label}")
        except Exception:
            pass
