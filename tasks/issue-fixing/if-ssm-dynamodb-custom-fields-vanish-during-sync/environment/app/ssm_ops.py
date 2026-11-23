# Parameter Store helpers for the account-metadata sync.
import os
from typing import Dict, List, Sequence

import boto3
from botocore.config import Config

from constants import DELETE_PARAMETER_BATCH_SIZE


def service_client(service: str, region: str = "us-east-1"):
    """Client for the account-factory's local AWS endpoint."""
    return boto3.client(
        service,
        region_name=region,
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL"),
        config=Config(retries={"max_attempts": 1, "mode": "standard"}),
    )


def put_ssm_parameters(client, parameters: Dict[str, str], parameter_path: str) -> None:
    """Create or update one String parameter per custom field."""
    for key, value in parameters.items():
        client.put_parameter(
            Name=parameter_path + key, Value=value, Type="String", Overwrite=True
        )


def get_ssm_parameters_names_by_path(client, path: str) -> List[str]:
    """List every parameter name under a path, following pagination."""
    paginator = client.get_paginator("get_parameters_by_path")
    pages = paginator.paginate(Path=path, Recursive=True)

    parameter_names: List[str] = []
    for page in pages:
        parameter_names.extend([param["Name"] for param in page["Parameters"]])

    return parameter_names


def delete_ssm_parameters(client, parameters: Sequence[str]) -> None:
    """Delete parameters by name, in batches of the API maximum."""
    names = list(parameters)
    for start in range(0, len(names), DELETE_PARAMETER_BATCH_SIZE):
        batch = names[start : start + DELETE_PARAMETER_BATCH_SIZE]
        client.delete_parameters(Names=batch)
