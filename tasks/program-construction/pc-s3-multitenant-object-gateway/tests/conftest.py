"""Shared fixtures: a fresh gateway (own metadata/account buckets) per test, plus tenants."""

import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.parse
import uuid

import boto3
import botocore
import pytest
from botocore import UNSIGNED
from botocore.config import Config
from harness import run_program, start_command

REGION = "us-east-1"
ACL_PREFIX = "vgw-meta-acl-"


def backend():
    """Floci with the backend test credentials."""
    return boto3.client("s3", config=Config(retries={"max_attempts": 2}))


def floci_ip_endpoint():
    url = urllib.parse.urlsplit(os.environ["AWS_ENDPOINT_URL"])
    return f"{url.scheme}://{socket.gethostbyname(url.hostname)}:{url.port}"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def error_of(call):
    """(HTTP status, S3 error code) of a failing call; fails the test if the call succeeds."""
    try:
        result = call()
    except botocore.exceptions.ClientError as exc:
        return exc.response["ResponseMetadata"]["HTTPStatusCode"], exc.response["Error"].get("Code")
    pytest.fail(f"request unexpectedly succeeded: {result!r}")


def bucket_contents(bucket):
    """Complete {key: bytes} map of an upstream bucket, read directly from Floci."""
    s3 = backend()
    found = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        for item in page.get("Contents", []):
            found[item["Key"]] = s3.get_object(Bucket=bucket, Key=item["Key"])["Body"].read()
    return found


def upstream_buckets():
    return {b["Name"] for b in backend().list_buckets().get("Buckets", [])}


def bucket_names(client):
    """ListBuckets names as a list (multiplicity preserved)."""
    return sorted(b["Name"] for b in client.list_buckets().get("Buckets", []))


def is_number(value):
    return type(value) in (int, float)


class Tenant:
    def __init__(self, gateway, access, secret):
        self.gateway, self.access, self.secret = gateway, access, secret
        self.s3 = gateway.client(access, secret)


class Gateway:
    def __init__(self, scratch):
        self.suffix = uuid.uuid4().hex[:12]
        self.meta = f"gwmeta-{self.suffix}"
        self.iamb = f"gwacct-{self.suffix}"
        self.root_access = f"root-{self.suffix}"
        self.root_secret = f"rootsecret-{self.suffix}"
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.scratch = scratch
        self.proc = None
        self.log = None

    def name(self, label):
        """A unique, valid bucket name owned by this test."""
        return f"{label}-{self.suffix}"

    def command(self):
        return [
            "/app/program", "--port", f"127.0.0.1:{self.port}",
            "--access", self.root_access, "--secret", self.root_secret, "--quiet",
            "--s3-iam-access", os.environ["AWS_ACCESS_KEY_ID"],
            "--s3-iam-secret", os.environ["AWS_SECRET_ACCESS_KEY"],
            "--s3-iam-region", REGION, "--s3-iam-bucket", self.iamb,
            "--s3-iam-endpoint", floci_ip_endpoint(),
            "s3", "--endpoint", os.environ["AWS_ENDPOINT_URL"], "--use-path-style",
            "--region", REGION,
            "--access", os.environ["AWS_ACCESS_KEY_ID"], "--secret", os.environ["AWS_SECRET_ACCESS_KEY"],
            "--meta-bucket", self.meta, "--disable-data-integrity-check",
        ]

    def launch(self):
        """Start the candidate gateway; return the process without waiting for readiness."""
        self.log = tempfile.TemporaryFile(dir=self.scratch)
        self.proc = start_command(self.command(), stdout=self.log, stderr=subprocess.STDOUT)
        return self.proc

    def log_text(self):
        if self.log is None:
            return ""
        self.log.seek(0)
        return self.log.read().decode(errors="replace")[-4000:]

    def start(self, timeout=20):
        self.launch()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                pytest.fail(f"gateway exited with {self.proc.returncode}: {self.log_text()}")
            try:
                socket.create_connection(("127.0.0.1", self.port), 0.2).close()
                return
            except OSError:
                time.sleep(0.1)
        pytest.fail(f"gateway did not listen on {self.url}: {self.log_text()}")

    def stop(self):
        if self.proc is not None:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait()
            self.proc = None
        if self.log is not None:
            self.log.close()
            self.log = None

    def client(self, access, secret, region=REGION):
        return boto3.client(
            "s3", endpoint_url=self.url, aws_access_key_id=access, aws_secret_access_key=secret,
            region_name=region, config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 1}),
        )

    def anonymous(self):
        return boto3.client(
            "s3", endpoint_url=self.url, region_name=REGION,
            config=Config(signature_version=UNSIGNED, s3={"addressing_style": "path"}, retries={"max_attempts": 1}),
        )

    def admin(self, *args, access=None, secret=None):
        return run_program(
            ["admin", "--access", access or self.root_access, "--secret", secret or self.root_secret,
             "--endpoint-url", self.url, *args],
            timeout=30,
        )

    def create_user(self, access, secret, *extra):
        result = self.admin("create-user", "--access", access, "--secret", secret, "--role", "userplus", *extra)
        assert result.returncode == 0, (result.stdout, result.stderr)
        return Tenant(self, access, secret)

    def users(self):
        """The decoded account store document read directly from Floci."""
        return json.loads(backend().get_object(Bucket=self.iamb, Key="users.json")["Body"].read())

    def acl_record(self, bucket):
        return json.loads(backend().get_object(Bucket=self.meta, Key=ACL_PREFIX + bucket)["Body"].read())

    def cleanup(self):
        s3 = backend()
        for name in upstream_buckets():
            if not name.endswith(self.suffix):
                continue
            try:
                for page in s3.get_paginator("list_objects_v2").paginate(Bucket=name):
                    for item in page.get("Contents", []):
                        s3.delete_object(Bucket=name, Key=item["Key"])
                s3.delete_bucket(Bucket=name)
            except botocore.exceptions.ClientError:
                pass


@pytest.fixture
def stopped_gateway():
    """Gateway configuration with no buckets created and no process started."""
    with tempfile.TemporaryDirectory() as scratch:
        gw = Gateway(scratch)
        try:
            yield gw
        finally:
            gw.stop()
            gw.cleanup()


@pytest.fixture
def gateway(stopped_gateway):
    """A running gateway with fresh, empty metadata and account buckets."""
    s3 = backend()
    s3.create_bucket(Bucket=stopped_gateway.meta)
    s3.create_bucket(Bucket=stopped_gateway.iamb)
    stopped_gateway.start()
    return stopped_gateway


@pytest.fixture
def tenants(gateway):
    """Two userplus tenants, created serially through the admin client."""
    a = gateway.create_user(f"tenanta-{gateway.suffix}", "secret-alpha-1")
    b = gateway.create_user(f"tenantb-{gateway.suffix}", "secret-bravo-2")
    return a, b
