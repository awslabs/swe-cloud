"""Scale up: turn a batch of queued workflow_job events into runner instances.

Port of lambdas/functions/control-plane/src/scale-runners/scale-up.ts (EC2 provider).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from . import config, fleet
from .ec2 import CreateScaleUpRunnersResult, Ec2RunnerConfig, create_runners
from .github import GitHubClient
from .github_runner import GitHubRunnerConfig
from .logger import add_persistent_context, create_child_logger

logger = create_child_logger("scale-up")

# One queued workflow_job event as it arrives from SQS, plus its SQS messageId:
# {"id", "eventType", "repositoryName", "repositoryOwner", "installationId", "repoOwnerType",
#  "labels", "retryCounter"?, "messageId"}
ActionRequestMessageSQS = dict[str, Any]


@dataclass
class MessagesWithClient:
    messages: list[ActionRequestMessageSQS] = field(default_factory=list)
    github_installation_client: GitHubClient | None = None
    runner_owner: str = ""


def get_current_runners(environment: str, runner_type: str, runner_owner: str) -> int:
    return len(fleet.list_ec2_runners(environment, runner_owner))


def scale_up(payloads: list[ActionRequestMessageSQS]) -> list[str]:
    """Returns the messageIds that must be retried through SQS."""
    logger.info("Received scale up requests", n_requests=len(payloads))

    enable_org_level = config.yn(os.environ.get("ENABLE_ORGANIZATION_RUNNERS"), True)
    maximum_runners = int(os.environ.get("RUNNERS_MAXIMUM_COUNT") or "3")
    runner_labels = os.environ.get("RUNNER_LABELS", "")
    runner_group = os.environ.get("RUNNER_GROUP_NAME") or "Default"
    ssm_token_path = os.environ.get("SSM_TOKEN_PATH", "")
    ephemeral_enabled = config.yn(os.environ.get("ENABLE_EPHEMERAL_RUNNERS"), False)
    enable_jit_config = config.yn(os.environ.get("ENABLE_JIT_CONFIG"), ephemeral_enabled)
    runner_name_prefix = os.environ.get("RUNNER_NAME_PREFIX", "")
    environment = os.environ.get("ENVIRONMENT", "")
    ec2_runner_config = Ec2RunnerConfig(environment=environment, scale_errors=config.scale_errors())

    # A map of either owner or owner/repo name to a code-host client, so we use a single client per
    # installation (set of messages), depending on how the app is installed.
    valid_messages: dict[str, MessagesWithClient] = {}
    retry_message_ids: dict[str, None] = {}  # insertion-ordered set
    for payload in payloads:
        event_type = payload.get("eventType")
        message_id = payload["messageId"]
        repository_owner = payload.get("repositoryOwner")
        repository_name = payload.get("repositoryName")
        if ephemeral_enabled and event_type != "workflow_job":
            logger.warn(
                "Event is not supported in combination with ephemeral runners. Please ensure you have enabled workflow_job events.",
                eventType=event_type,
                messageId=message_id,
            )
            retry_message_ids[message_id] = None
            continue

        if not is_valid_repo_owner_type_if_org_level_enabled(payload, enable_org_level):
            logger.warn(
                "Repository does not belong to an organization and organization runners are enabled. "
                "This is not supported. Not scaling up for this event. Not throwing error to prevent "
                "re-queueing and just ignoring the event.",
                repository=f"{repository_owner}/{repository_name}",
                messageId=message_id,
            )
            continue

        runner_owner = repository_owner if enable_org_level else f"{repository_owner}/{repository_name}"

        key = runner_owner
        labels = payload.get("labels") or []
        if any(label.startswith("ghr-") for label in labels):
            key = f"{key}/{labels_hash(labels)}"

        entry = valid_messages.get(key)
        # If we've not seen this owner/repo before, we'll need to create a code-host client for it.
        if entry is None:
            entry = MessagesWithClient(
                messages=[],
                github_installation_client=GitHubClient(int(payload.get("installationId") or 0)),
                runner_owner=runner_owner,
            )
            valid_messages[key] = entry

        entry.messages.append(payload)

    runner_type = "Org" if enable_org_level else "Repo"
    add_persistent_context(
        runner={
            "ephemeral": ephemeral_enabled,
            "type": runner_type,
            "namePrefix": runner_name_prefix,
            "n_events": sum(len(group.messages) for group in valid_messages.values()),
        }
    )
    logger.info("Received events")

    for group, entry in valid_messages.items():
        messages = entry.messages
        runner_owner = entry.runner_owner
        # Work out how much we want to scale up by.
        scale_up_count = 0
        queued_messages: list[ActionRequestMessageSQS] = []

        # Reset per group to avoid accumulating labels across iterations
        group_runner_labels = runner_labels
        dynamic_labels = [label for label in (messages[0].get("labels") or []) if label.startswith("ghr-")]
        if dynamic_labels:
            group_runner_labels = f"{group_runner_labels},{','.join(dynamic_labels)}" if group_runner_labels else ",".join(dynamic_labels)

        for message in messages:
            scale_up_count += 1
            queued_messages.append(message)

        if scale_up_count == 0:
            logger.info("No runners will be created for this group, no valid messages found.")
            continue

        # Don't query the provider if we can create an unlimited number of runners.
        current_runners = 0 if maximum_runners == -1 else get_current_runners(environment, runner_type, runner_owner)

        logger.info("Current runners", currentRunners=current_runners, maximumRunners=maximum_runners, group=group)

        # Calculate how many runners we want to create. Use max(0, ...) to ensure we never attempt
        # to create a negative number of runners, which can happen when current_runners exceeds
        # maximum_runners due to pool/scale-up race conditions.
        new_runners = (
            scale_up_count
            if maximum_runners == -1
            else max(0, min(scale_up_count, maximum_runners - current_runners))
        )

        skipped_runner_count = max(0, scale_up_count - new_runners)

        if skipped_runner_count > 0:
            logger.info(
                "Not all runners will be created for this group, maximum number of runners reached.",
                desiredNewRunners=scale_up_count,
            )

            if ephemeral_enabled:
                # This removes `skipped_runner_count` items from the start of the list so that, if
                # we retry more messages later, we pick fresh ones.
                removed_messages = messages[:skipped_runner_count]
                del messages[:skipped_runner_count]
                for removed in removed_messages:
                    retry_message_ids[removed["messageId"]] = None

            # No runners will be created, so skip calling the provider.
            if new_runners <= 0:
                continue

        logger.info("Attempting to launch new runners", newRunners=new_runners)

        github_runner_config = GitHubRunnerConfig(
            ephemeral=ephemeral_enabled,
            enable_jit_config=enable_jit_config,
            runner_labels=group_runner_labels,
            runner_group=runner_group,
            runner_name_prefix=runner_name_prefix,
            runner_owner=runner_owner,
            runner_type=runner_type,
            ssm_token_path=ssm_token_path,
        )

        job_ids = [message["id"] for message in messages[:new_runners]]
        try:
            create_runners_result = create_runners(
                github_runner_config,
                ec2_runner_config,
                job_ids,
                entry.github_installation_client,
            )
        except Exception as error:  # noqa: BLE001
            logger.error(
                "Runner provider threw an unexpected error.",
                error=str(error),
                retryable=True,
                failedMessageCount=new_runners,
            )
            create_runners_result = CreateScaleUpRunnersResult([], new_runners, 0)

        logger.info(
            "Runner creation summary.",
            requestedMessageCount=new_runners,
            successfulRunnerCount=len(create_runners_result.instances),
            retryableErrorCount=create_runners_result.retryable_error_count,
            nonRetryableErrorCount=create_runners_result.non_retryable_error_count,
        )

        if create_runners_result.non_retryable_error_count > 0:
            logger.warn(
                "Some runner creation messages will not be retried through the current SQS batch.",
                nonRetryableErrorCount=create_runners_result.non_retryable_error_count,
            )

        if create_runners_result.retryable_error_count > 0:
            failed_messages = messages[: create_runners_result.retryable_error_count]
            for failed in failed_messages:
                retry_message_ids[failed["messageId"]] = None

    return list(retry_message_ids)


def is_valid_repo_owner_type_if_org_level_enabled(payload: ActionRequestMessageSQS, enable_org_level: bool) -> bool:
    return not (enable_org_level and payload.get("repoOwnerType") != "Organization")


def labels_hash(labels: list[str]) -> str:
    prefix = "ghr-"
    text = "|".join(sorted(label for label in labels if label.startswith(prefix)))  # deterministic hash
    value = 0
    for char in text:
        value = ((value << 5) - value + ord(char)) & 0xFFFFFFFF
    if value & 0x80000000:
        value -= 1 << 32
    return _base36(abs(value))


def _base36(number: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        number, rem = divmod(number, 36)
        out = digits[rem] + out
        if number == 0:
            return out
