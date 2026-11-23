"""boto3 clients for the sweep.

The endpoint comes from AWS_ENDPOINT_URL (the scheduled sweep points it at the account gateway).
Client-side retries are off: the queue is the retry loop, as in aws-nuke, so a throttled or
refused call fails the item and the item is retried on the next pass.
"""
import functools
import os

import boto3
from botocore.config import Config

REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

_CONFIG = Config(
    retries={"total_max_attempts": 1},
    s3={"addressing_style": "path"},
    connect_timeout=5,
    read_timeout=30,
)


@functools.lru_cache(maxsize=None)
def client(service):
    return boto3.client(service, region_name=REGION, endpoint_url=os.environ.get("AWS_ENDPOINT_URL"),
                        config=_CONFIG)


def error_code(err):
    response = getattr(err, "response", None) or {}
    return response.get("Error", {}).get("Code", "")
