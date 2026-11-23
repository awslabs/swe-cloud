"""EC2 access for runners, through the fleet API.

The fleet API is the account's thin front for EC2 (CreateFleet / DescribeInstances /
TerminateInstances with the module's launch template and ghr:* tags applied). It is JSON over HTTP
at FLEET_API_URL; see README.md for the endpoints. Every instance it launches is bound to the
workflow job it was requested for, and carries that job id for its whole life.

Port of lambdas/functions/control-plane/src/aws/ec2-runners.ts.
"""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from . import config
from .logger import create_child_logger

logger = create_child_logger("ec2-runners")

# Client-side retry policy, as the AWS SDK's standard mode applies it to EC2 calls.
MAX_ATTEMPTS = 3
BASE_DELAY_S = 0.05
REQUEST_TIMEOUT_S = 2.0

RETRYABLE_AWS_ERROR_NAMES = {
    "EC2ThrottledException",
    "InternalError",
    "RequestLimitExceeded",
    "RequestTimeout",
    "RequestTimeoutException",
    "ServiceUnavailable",
    "Throttling",
    "ThrottlingException",
}


class FleetApiError(Exception):
    """An error answer from the fleet API (the EC2 error, passed through)."""

    def __init__(self, name: str, message: str, status: int | None) -> None:
        super().__init__(f"{name}: {message}")
        self.name = name
        self.status = status
        self.fault = "server" if status is not None and status >= 500 else "client"


@dataclass
class RunnerInfo:
    instance_id: str
    owner: str
    job_id: int | None
    state: str
    launch_time: str | None = None


@dataclass
class CreateRunnerResult:
    instances: list[str] = field(default_factory=list)
    retryable_error_count: int = 0
    non_retryable_error_count: int = 0


def _post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    url = f"{config.fleet_api_url()}{path}"
    data = json.dumps(body).encode()
    attempt = 0
    while True:
        attempt += 1
        try:
            request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as http_error:
            try:
                payload = json.loads(http_error.read() or b"{}")
            except ValueError:
                payload = {}
            error = FleetApiError(payload.get("__type", "UnknownError"), payload.get("message", str(http_error)), http_error.code)
            retryable = http_error.code == 429 or http_error.code >= 500
        except (urllib.error.URLError, TimeoutError, ConnectionError) as network_error:
            error = FleetApiError("NetworkingError", str(network_error), None)
            retryable = True
        if not retryable or attempt >= MAX_ATTEMPTS:
            raise error
        time.sleep(random.uniform(0, BASE_DELAY_S * 2 ** (attempt - 1)))


def is_retryable_aws_error(error: BaseException, configured_retryable_errors: list[str]) -> bool:
    if not isinstance(error, Exception):
        return False
    name = getattr(error, "name", type(error).__name__)
    if is_retryable_aws_error_name(name, configured_retryable_errors):
        return True
    status = getattr(error, "status", None)
    if getattr(error, "fault", None) == "server" or status == 429 or (status is not None and status >= 500):
        return True
    if name == "NetworkingError":
        return True
    cause = error.__cause__
    if cause is not None and cause is not error:
        return is_retryable_aws_error(cause, configured_retryable_errors)
    return False


def is_retryable_aws_error_name(error_name: str, configured_retryable_errors: list[str]) -> bool:
    return error_name in configured_retryable_errors or error_name in RETRYABLE_AWS_ERROR_NAMES


def list_ec2_runners(environment: str, runner_owner: str) -> list[RunnerInfo]:
    """Running and pending runner instances of one owner (DescribeInstances by ghr:* tags)."""
    answer = _post("/describe-runners", {"environment": environment, "owner": runner_owner})
    return [
        RunnerInfo(
            instance_id=item["instance_id"],
            owner=item.get("owner", runner_owner),
            job_id=item.get("job_id"),
            state=item.get("state", "running"),
            launch_time=item.get("launch_time"),
        )
        for item in answer.get("instances", [])
    ]


def terminate_runner(instance_id: str) -> None:
    logger.debug(f"Runner '{instance_id}' will be terminated.")
    _post("/terminate", {"instance_id": instance_id})
    logger.debug(f"Runner {instance_id} has been terminated.")


def create_runner(environment: str, runner_owner: str, job_ids: list[int], scale_errors: list[str]) -> CreateRunnerResult:
    """Launch one runner instance per job id with a single CreateFleet request."""
    number_of_runners = len(job_ids)
    try:
        fleet = _post("/create-fleet", {"environment": environment, "owner": runner_owner, "job_ids": job_ids})
    except Exception as error:  # noqa: BLE001 - classified below, like the SDK error
        retryable = is_retryable_aws_error(error, scale_errors)
        logger.warn("Create fleet request failed.", error=str(error), retryable=retryable)
        return _failed_create_runner_result(number_of_runners, retryable)

    result = _process_fleet_result(fleet, number_of_runners, scale_errors)
    logger.info(f"Created instance(s): {','.join(result.instances)}")
    return result


def _process_fleet_result(fleet: dict[str, Any], number_of_runners: int, scale_errors: list[str]) -> CreateRunnerResult:
    instances = [item["instance_id"] for item in fleet.get("instances", [])]

    if len(instances) == number_of_runners:
        return CreateRunnerResult(instances=instances)

    logger.warn(
        f"{'No' if not instances else f'{len(instances)} off {number_of_runners}'} instances created.",
        data=fleet,
    )

    errors = fleet.get("errors", [])
    triggering_retry = [e for e in errors if is_retryable_aws_error_name(e.get("error_code", ""), scale_errors)]
    not_triggering_retry = [e for e in errors if e not in triggering_retry]

    missing_instance_count = number_of_runners - len(instances)
    # CreateFleet errors describe failed launch-template overrides, not individual instances.
    # A retryable override failure can therefore account for any number of missing instances.
    retryable_error_count = missing_instance_count if triggering_retry else 0
    non_retryable_error_count = missing_instance_count - retryable_error_count

    logger.warn(
        "Create fleet did not create every requested instance.",
        data=errors,
        retryableErrorCount=retryable_error_count,
        nonRetryableErrorCount=non_retryable_error_count,
        fleetErrorsTriggeringRetry=triggering_retry,
        fleetErrorsNotTriggeringRetry=not_triggering_retry,
    )
    return CreateRunnerResult(instances, retryable_error_count, non_retryable_error_count)


def _failed_create_runner_result(failed_instance_count: int, is_retryable: bool) -> CreateRunnerResult:
    return CreateRunnerResult(
        instances=[],
        retryable_error_count=failed_instance_count if is_retryable else 0,
        non_retryable_error_count=0 if is_retryable else failed_instance_count,
    )
