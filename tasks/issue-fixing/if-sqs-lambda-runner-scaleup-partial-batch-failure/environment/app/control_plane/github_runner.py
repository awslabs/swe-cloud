"""Code-host side of runner creation: JIT configs, written to Parameter Store for the instance to fetch.

Port of lambdas/functions/control-plane/src/scale-runners/github-runner.ts (JIT path).
"""

from __future__ import annotations

from dataclasses import dataclass

from . import ssm
from .github import GitHubClient
from .logger import create_child_logger

logger = create_child_logger("github-runner")


@dataclass
class GitHubRunnerConfig:
    ephemeral: bool
    enable_jit_config: bool
    runner_labels: str
    runner_group: str
    runner_name_prefix: str
    runner_owner: str
    runner_type: str
    ssm_token_path: str


def get_runner_group_id(github_runner_config: GitHubRunnerConfig, gh_client: GitHubClient) -> int:
    # The default group always has id 1; named groups are not used by this deployment.
    return 1


def create_start_runner_config(
    github_runner_config: GitHubRunnerConfig,
    instance_ids: list[str],
    gh_client: GitHubClient,
) -> list[str]:
    """Create the start config for each instance; returns the instance ids that failed."""
    if github_runner_config.enable_jit_config and github_runner_config.ephemeral:
        return create_jit_config(github_runner_config, instance_ids, gh_client)
    raise NotImplementedError("registration-token runners are not deployed here")


def create_jit_config(
    github_runner_config: GitHubRunnerConfig,
    runner_ids: list[str],
    gh_client: GitHubClient,
) -> list[str]:
    """Creates JIT configuration for ephemeral runners.

    Continues processing remaining runners even if some fail. Returns the runner (instance) ids
    that failed to get a JIT configuration.
    """
    runner_group_id = get_runner_group_id(github_runner_config, gh_client)
    runner_labels = github_runner_config.runner_labels.split(",")
    failed_runner_ids: list[str] = []

    for runner_id in runner_ids:
        try:
            # generate jit config for runner registration
            runner_name = f"{github_runner_config.runner_name_prefix}{runner_id}"
            runner_config = gh_client.generate_runner_jitconfig_for_org(
                org=github_runner_config.runner_owner,
                name=runner_name,
                runner_group_id=runner_group_id,
                labels=runner_labels,
            )
            logger.debug("Runner JIT config for ephemeral runner generated.", instance=runner_id)
            # store jit config in ssm parameter store
            ssm.put_parameter(f"{github_runner_config.ssm_token_path}/{runner_id}", runner_config["encoded_jit_config"], True)
        except Exception as error:  # noqa: BLE001
            failed_runner_ids.append(runner_id)
            logger.warn(
                "Failed to create JIT config for instance, continuing with remaining instances",
                instance=runner_id,
                error=str(error),
                retryable=True,
            )

    if failed_runner_ids:
        logger.error(
            "Failed to create JIT config for some instances",
            failedInstances=failed_runner_ids,
            totalInstances=len(runner_ids),
            successfulInstances=len(runner_ids) - len(failed_runner_ids),
            retryable=True,
        )

    return failed_runner_ids
