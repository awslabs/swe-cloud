"""Runner creation on EC2: launch, register with the code host, clean up what could not be configured.

Port of lambdas/functions/control-plane/src/scale-runners/ec2.ts.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import fleet
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
    instances: list[str] = field(default_factory=list)
    retryable_error_count: int = 0
    non_retryable_error_count: int = 0


def create_runners(
    github_runner_config: GitHubRunnerConfig,
    ec2_runner_config: Ec2RunnerConfig,
    job_ids: list[int],
    gh_client: GitHubClient,
) -> CreateScaleUpRunnersResult:
    number_of_runners = len(job_ids)
    try:
        created = fleet.create_runner(
            ec2_runner_config.environment,
            github_runner_config.runner_owner,
            job_ids,
            ec2_runner_config.scale_errors,
        )
        result = CreateScaleUpRunnersResult(
            created.instances, created.retryable_error_count, created.non_retryable_error_count
        )
    except Exception as error:  # noqa: BLE001
        logger.error(
            "Unexpected error while creating EC2 runner instances.",
            error=str(error),
            retryable=True,
            failedInstanceCount=number_of_runners,
        )
        return CreateScaleUpRunnersResult([], number_of_runners, 0)

    if result.instances:
        try:
            failed_instances = create_start_runner_config(github_runner_config, result.instances, gh_client)
        except Exception as error:  # noqa: BLE001
            logger.error(
                "Unexpected error while registering CI runners.",
                error=str(error),
                retryable=True,
                failedInstances=result.instances,
                failedInstanceCount=len(result.instances),
            )
            failed_instances = list(result.instances)

        # Terminate instances that failed to get configured to avoid waste
        if failed_instances:
            logger.warn(
                "Terminating instances that failed to get configured",
                failedInstances=failed_instances,
                failedInstanceCount=len(failed_instances),
                retryable=True,
            )

            terminate_failed_instances(failed_instances)

            return CreateScaleUpRunnersResult(
                instances=[i for i in result.instances if i not in failed_instances],
                retryable_error_count=result.retryable_error_count + len(failed_instances),
                non_retryable_error_count=result.non_retryable_error_count,
            )

    return result


def terminate_failed_instances(instance_ids: list[str]) -> None:
    for instance_id in instance_ids:
        try:
            fleet.terminate_runner(instance_id)
        except Exception as error:  # noqa: BLE001
            logger.error("Failed to terminate instance", instanceId=instance_id, error=str(error))
