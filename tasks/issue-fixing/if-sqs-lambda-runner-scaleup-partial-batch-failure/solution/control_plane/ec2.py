"""Runner creation on EC2: launch, register with the code host, clean up what could not be configured.

Port of lambdas/functions/control-plane/src/scale-runners/ec2.ts. Results are per job: the fleet
binds every instance to the job it was launched for, so which job is retried follows from which
instance failed, not from a count.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import fleet, ssm
from .github import GitHubClient
from .github_runner import GitHubRunnerConfig, create_start_runner_config
from .logger import create_child_logger

logger = create_child_logger("ec2-runners")


@dataclass
class Ec2RunnerConfig:
    environment: str
    scale_errors: list[str]


@dataclass
class CreateScaleUpRunnersResult:
    configured: list[Any] = field(default_factory=list)  # job ids that now have a configured runner
    retry: list[Any] = field(default_factory=list)  # job ids to hand back to SQS
    dropped: list[Any] = field(default_factory=list)  # job ids that failed for good (not retried)


def create_runners(
    github_runner_config: GitHubRunnerConfig,
    ec2_runner_config: Ec2RunnerConfig,
    job_ids: list[Any],
    gh_client: GitHubClient,
) -> CreateScaleUpRunnersResult:
    result = CreateScaleUpRunnersResult()
    try:
        created = fleet.create_runner(
            ec2_runner_config.environment,
            github_runner_config.runner_owner,
            job_ids,
            ec2_runner_config.scale_errors,
        )
    except Exception as error:  # noqa: BLE001
        logger.error("Unexpected error while creating EC2 runner instances.", error=str(error), retryable=True,
                     failedInstanceCount=len(job_ids))
        result.retry = list(job_ids)
        return result

    launched_jobs = set(created.jobs.values())
    missing = [job_id for job_id in job_ids if job_id not in launched_jobs]
    if missing:
        (result.retry if created.retryable_error_count > 0 else result.dropped).extend(missing)

    for instance_id in created.instances:
        job_id = created.jobs.get(instance_id)
        if configure_instance(github_runner_config, instance_id, gh_client):
            result.configured.append(job_id)
        else:
            result.retry.append(job_id)
            discard_instance(instance_id)
    return result


def configure_instance(github_runner_config: GitHubRunnerConfig, instance_id: str, gh_client: GitHubClient) -> bool:
    """JIT config for one instance, written to SSM. True once the instance can register."""
    try:
        return not create_start_runner_config(github_runner_config, [instance_id], gh_client)
    except Exception as error:  # noqa: BLE001
        logger.error("Unexpected error while registering CI runner.", instance=instance_id, error=str(error))
        return False


def is_configured(github_runner_config: GitHubRunnerConfig, instance_id: str) -> bool | None:
    """Whether the instance's runner config is in SSM; None when SSM cannot tell right now."""
    try:
        return ssm.parameter_exists(f"{github_runner_config.ssm_token_path}/{instance_id}")
    except Exception as error:  # noqa: BLE001
        logger.warn("Could not read runner config", instance=instance_id, error=str(error))
        return None


def discard_instance(instance_id: str) -> bool:
    """Terminate an instance that has no runner config. A failure leaves it running, still bound to
    its job, which goes back to SQS: the next delivery of that job finds it through describe-runners
    and finishes or terminates it."""
    try:
        fleet.terminate_runner(instance_id)
        return True
    except Exception as error:  # noqa: BLE001
        logger.error("Failed to terminate instance; it will be reconciled on the job's next delivery",
                     instanceId=instance_id, error=str(error))
        return False
