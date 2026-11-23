"""boto3 clients. Endpoint, region and credentials come from the standard SDK environment
(AWS_ENDPOINT_URL, AWS_DEFAULT_REGION, AWS_ACCESS_KEY_ID, ...)."""

from __future__ import annotations

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, ConnectionError, HTTPClientError

_CONFIG = Config(retries={"max_attempts": 10, "mode": "standard"})
# One attempt per call: for writes the SDK must not send again on its own (see parameter_store._delete).
_ONCE = Config(retries={"total_max_attempts": 1, "mode": "standard"})


def client(service: str, *, sdk_retries: bool = True):
    return boto3.client(service, config=_CONFIG if sdk_retries else _ONCE)


def outcome_unknown(exc: BaseException) -> bool:
    """The call may or may not have been applied: the connection failed before a reply arrived."""
    return isinstance(exc, (ConnectionError, HTTPClientError))


def error_code(exc: BaseException) -> str | None:
    if isinstance(exc, ClientError):
        return exc.response.get("Error", {}).get("Code")
    return None
