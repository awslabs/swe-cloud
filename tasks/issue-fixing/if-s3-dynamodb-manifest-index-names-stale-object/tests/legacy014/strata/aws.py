"""AWS client construction shared by every strata process."""

import os

import boto3
from botocore.config import Config

DEFAULT_TABLE = "strata-manifests"
DEFAULT_BUCKET = "strata-warehouse"


def _config():
    return Config(
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        s3={"addressing_style": "path"},
        retries={"mode": "standard", "max_attempts": 3},
        connect_timeout=5,
        read_timeout=60,
    )


def endpoint_url():
    # Every AWS call goes through the gateway named here; see docs/OPERATIONS.md.
    return os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566")


def s3_client():
    return boto3.client("s3", endpoint_url=endpoint_url(), config=_config())


def dynamodb_client():
    return boto3.client("dynamodb", endpoint_url=endpoint_url(), config=_config())


def table_name():
    return os.environ.get("STRATA_TABLE", DEFAULT_TABLE)


def bucket_name():
    return os.environ.get("STRATA_BUCKET", DEFAULT_BUCKET)
