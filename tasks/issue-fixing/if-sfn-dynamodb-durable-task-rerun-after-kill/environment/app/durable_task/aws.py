"""boto3 clients for the worker.

Endpoint, region and credentials come from the standard AWS environment (``AWS_ENDPOINT_URL``,
``AWS_DEFAULT_REGION``, ...), which is how the scheduler points each try at its account.
"""
import boto3
from botocore.config import Config

_CONFIG = Config(
    retries={"mode": "standard", "max_attempts": 3},
    connect_timeout=5,
    read_timeout=30,
)


def client(service: str):
    return boto3.client(service, config=_CONFIG)
