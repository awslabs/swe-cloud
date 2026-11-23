"""Parameter Store access (port of @aws-github-runner/aws-ssm-util).

The client takes its endpoint from the standard SDK environment: in the VPC the function reaches
SSM through the interface endpoint named by AWS_ENDPOINT_URL_SSM.
"""

from __future__ import annotations

import functools

import boto3
from botocore.config import Config

# The SDK's standard retry mode, 3 attempts in total, as the Lambda's v3 SDK makes them.
_CONFIG = Config(retries={"total_max_attempts": 3, "mode": "standard"}, connect_timeout=2, read_timeout=3)


@functools.lru_cache(maxsize=1)
def client():
    return boto3.client("ssm", config=_CONFIG)


def put_parameter(name: str, value: str, secure: bool, overwrite: bool = False) -> None:
    client().put_parameter(Name=name, Value=value, Type="SecureString" if secure else "String", Overwrite=overwrite)


def get_parameter(name: str) -> str:
    return client().get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]


def parameter_exists(name: str) -> bool:
    """True if the parameter exists, False if it does not; other errors propagate."""
    try:
        client().get_parameter(Name=name, WithDecryption=False)
    except client().exceptions.ParameterNotFound:
        return False
    return True


def parameters_by_path(path: str) -> dict[str, str]:
    """Every parameter directly under `path` (name -> value)."""
    values: dict[str, str] = {}
    kwargs = {"Path": path, "WithDecryption": True}
    while True:
        page = client().get_parameters_by_path(**kwargs)
        values.update({p["Name"]: p["Value"] for p in page.get("Parameters", [])})
        if not page.get("NextToken"):
            return values
        kwargs["NextToken"] = page["NextToken"]


def delete_parameter(name: str) -> None:
    client().delete_parameter(Name=name)
