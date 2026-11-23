"""boto3 clients. Endpoint, region and credentials come from the standard SDK environment
(AWS_ENDPOINT_URL, AWS_DEFAULT_REGION, AWS_ACCESS_KEY_ID, ...)."""

from __future__ import annotations

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

_CONFIG = Config(retries={"max_attempts": 10, "mode": "standard"})


def client(service: str):
    return boto3.client(service, config=_CONFIG)


def error_code(exc: BaseException) -> str | None:
    if isinstance(exc, ClientError):
        return exc.response.get("Error", {}).get("Code")
    return None
