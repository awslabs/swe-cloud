"""Shared fixtures: per-test AWS resources with cleanup, a candidate workspace, typed comparison."""

import uuid

import boto3
import pytest
import yaml

from checks import json_stream
from harness import run_program, workspace, write_fixture


class Aws:
    """Creates uniquely named SSM/Secrets Manager/KMS resources and removes them afterwards."""

    def __init__(self):
        self.prefix = "rx" + uuid.uuid4().hex[:12]
        self._cleanup = []

    def client(self, service, region=None):
        return boto3.client(service, region_name=region) if region else boto3.client(service)

    # SSM: `name` is relative to the test prefix; the returned path has no leading slash.
    def param(self, name, value, kind="String", region=None, overwrite=False):
        full = f"/{self.prefix}/{name}"
        self.client("ssm", region).put_parameter(Name=full, Value=value, Type=kind, Overwrite=overwrite)
        if not overwrite:
            self._cleanup.append(("ssm", region, full))
        return full[1:]

    def secret(self, name, string=None, binary=None, region=None):
        full = name if name.startswith("/") else f"{self.prefix}/{name}"
        kwargs = {"SecretString": string} if string is not None else {"SecretBinary": binary}
        out = self.client("secretsmanager", region).create_secret(Name=full, **kwargs)
        self._cleanup.append(("secret", region, full))
        return full, out

    def put_secret(self, full, string, stages=None):
        kwargs = {"VersionStages": stages} if stages else {}
        return self.client("secretsmanager").put_secret_value(SecretId=full, SecretString=string, **kwargs)

    def key(self):
        meta = self.client("kms").create_key()["KeyMetadata"]
        self._cleanup.append(("key", None, meta["KeyId"]))
        return meta

    def alias(self, key_id):
        name = f"alias/{self.prefix}"
        self.client("kms").create_alias(AliasName=name, TargetKeyId=key_id)
        self._cleanup.insert(0, ("alias", None, name))
        return name

    def encrypt(self, key_id, plaintext, context=None):
        kwargs = {"EncryptionContext": context} if context else {}
        blob = self.client("kms").encrypt(KeyId=key_id, Plaintext=plaintext, **kwargs)["CiphertextBlob"]
        return blob

    def close(self):
        for kind, region, ident in reversed(self._cleanup):
            try:
                if kind == "ssm":
                    self.client("ssm", region).delete_parameter(Name=ident)
                elif kind == "secret":
                    self.client("secretsmanager", region).delete_secret(SecretId=ident, ForceDeleteWithoutRecovery=True)
                elif kind == "alias":
                    self.client("kms").delete_alias(AliasName=ident)
                elif kind == "key":
                    self.client("kms").schedule_key_deletion(KeyId=ident, PendingWindowInDays=7)
            except Exception as error:  # noqa: BLE001 -- best-effort cleanup must not mask results
                print(f"cleanup {kind} {ident}: {error}")


@pytest.fixture
def aws():
    resources = Aws()
    try:
        yield resources
    finally:
        resources.close()


class Cli:
    """Runs the public executable with documents written into a candidate workspace."""

    def __init__(self, root):
        self.root = root
        self._count = 0

    def file(self, text, name=None):
        self._count += 1
        path = self.root / (name or f"doc{self._count}.yaml")
        write_fixture(path, text)
        return path

    def run(self, *args, stdin=None, env=None):
        return run_program(list(args), cwd=self.root, stdin=stdin, env=env)

    def eval(self, text, *flags, fmt="json"):
        """Evaluate `text`; returns (process, decoded documents or None)."""
        path = self.file(text)
        proc = self.run("eval", "-s", *flags, "-f", path, "-o", fmt)
        docs = None
        if proc.returncode == 0:
            docs = json_stream(proc.stdout) if fmt == "json" else list(yaml.safe_load_all(proc.stdout))
        return proc, docs

    def get(self, expression):
        return self.run("get", "-s", expression)


@pytest.fixture
def cli():
    with workspace() as root:
        yield Cli(root)
