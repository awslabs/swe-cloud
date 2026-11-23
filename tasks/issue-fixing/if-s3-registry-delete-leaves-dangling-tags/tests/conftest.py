import os
import secrets
import subprocess
import sys

import pytest
import boto3
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

import harness  # noqa: E402
from cdk_seed.cfn_deploy import deploy

BASELINE = {}


def aws_resources():
    import boto3
    kw = dict(endpoint_url=harness.FLOCI, region_name="us-east-1",
              aws_access_key_id="test", aws_secret_access_key="test")
    out = {}
    try:
        out["dynamodb"] = set(boto3.client("dynamodb", **kw).list_tables().get("TableNames", []))
    except Exception:
        out["dynamodb"] = set()
    try:
        out["sqs"] = set(boto3.client("sqs", **kw).list_queues().get("QueueUrls", []))
    except Exception:
        out["sqs"] = set()
    try:
        out["ssm"] = {p["Name"] for p in
                      boto3.client("ssm", **kw).describe_parameters().get("Parameters", [])}
    except Exception:
        out["ssm"] = set()
    try:
        out["s3"] = {b["Name"] for b in boto3.client("s3", **kw).list_buckets().get("Buckets", [])}
    except Exception:
        out["s3"] = set()
    return out


@pytest.fixture(scope="session")
def bucket():
    # Replicas left running from earlier work would hold ports and write to the emulator.
    subprocess.run(["pkill", "-9", "-f", "[m] quayside"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    name = f"registry-{secrets.token_hex(3)}"
    deploy(boto3.client("cloudformation", endpoint_url=harness.FLOCI,
                        region_name="us-east-1", aws_access_key_id="test",
                        aws_secret_access_key="test"),
           "seed-" + name, Path(__file__).parent / "cdk_seed/seed.template.json",
           BucketName=name)
    BASELINE.update(aws_resources())
    BASELINE["bucket"] = name
    return name


@pytest.fixture
def cluster(bucket, request):
    cl = harness.Cluster(bucket, request.node.name)
    try:
        cl.start()
        yield cl
    finally:
        cl.stop()
