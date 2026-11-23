"""Scale up: turn a batch of queued workflow_job events into runner instances.

Port of lambdas/functions/control-plane/src/scale-runners/scale-up.ts (EC2 provider).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable

from . import config, fleet, launches
from .ec2 import Ec2RunnerConfig, configure_instance, create_runners, discard_instance, is_configured
from .github import GitHubClient
from .github_runner import GitHubRunnerConfig
from .logger import add_persistent_context, create_child_logger

logger = create_child_logger("scale-up")

# Per-message outcomes. RETRY goes back to SQS; everything else is acknowledged.
CONFIGURED = "configured"  # the job has a configured runner
RETRY = "retry"
IGNORED = "ignored"  # not an event this deployment serves
DROPPED = "dropped"  # runner creation failed for good

GROUP_TIME_RESERVE_MS = 2500

# One queued workflow_job event as it arrives from SQS, plus its SQS messageId:
# {"id", "eventType", "repositoryName", "repositoryOwner", "installationId", "repoOwnerType",
#  "labels", "retryCounter"?, "messageId"}
ActionRequestMessageSQS = dict[str, Any]


@dataclass
class MessagesWithClient:
    messages: list[ActionRequestMessageSQS] = field(default_factory=list)
    github_installation_client: GitHubClient | None = None
    runner_owner: str = ""


def scale_up(
    payloads: list[ActionRequestMessageSQS],
    outcomes: dict[str, str] | None = None,
    remaining_ms: Callable[[], int] = lambda: 10**9,
) -> list[str]:
    """Returns the messageIds that must be retried through SQS.

    `outcomes` (messageId -> outcome) is filled in as records are decided, so a caller that sees an
    exception still knows which records already have runners.
    """
    outcomes = {} if outcomes is None else outcomes
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
            outcomes[message_id] = RETRY
            continue

        if not is_valid_repo_owner_type_if_org_level_enabled(payload, enable_org_level):
            logger.warn(
                "Repository does not belong to an organization and organization runners are enabled. "
                "This is not supported. Not scaling up for this event. Not throwing error to prevent "
                "re-queueing and just ignoring the event.",
                repository=f"{repository_owner}/{repository_name}",
                messageId=message_id,
            )
            outcomes[message_id] = IGNORED
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
        if remaining_ms() < GROUP_TIME_RESERVE_MS:
            logger.warn("Not enough time left in this invocation for another group; returning it to SQS", group=group)
            continue
        github_runner_config = GitHubRunnerConfig(
            ephemeral=ephemeral_enabled,
            enable_jit_config=enable_jit_config,
            runner_labels=group_runner_labels_for(runner_labels, entry.messages),
            runner_group=runner_group,
            runner_name_prefix=runner_name_prefix,
            runner_owner=entry.runner_owner,
            runner_type=runner_type,
            ssm_token_path=ssm_token_path,
        )
        try:
            scale_up_group(entry, github_runner_config, ec2_runner_config, maximum_runners, ephemeral_enabled, outcomes)
        except Exception as error:  # noqa: BLE001 - records already decided keep their outcome
            logger.error("Unexpected error while scaling up group; undecided messages go back to SQS", group=group, error=repr(error))

    return [p["messageId"] for p in payloads if outcomes.get(p["messageId"], RETRY) == RETRY]


def group_runner_labels_for(runner_labels: str, messages: list[ActionRequestMessageSQS]) -> str:
    dynamic_labels = [label for label in (messages[0].get("labels") or []) if label.startswith("ghr-")]
    if not dynamic_labels:
        return runner_labels
    return f"{runner_labels},{','.join(dynamic_labels)}" if runner_labels else ",".join(dynamic_labels)


def scale_up_group(
    entry: MessagesWithClient,
    github_runner_config: GitHubRunnerConfig,
    ec2_runner_config: Ec2RunnerConfig,
    maximum_runners: int,
    ephemeral_enabled: bool,
    outcomes: dict[str, str],
) -> None:
    runner_owner = entry.runner_owner
    by_job: dict = {}
    for message in entry.messages:
        by_job.setdefault(message["id"], []).append(message)

    def decide(job_id, outcome: str) -> None:
        for message in by_job.get(job_id, []):
            outcomes[message["messageId"]] = outcome

    # Cap check. Without a count we cannot know how many runners the cap allows, so the whole
    # group goes back to SQS (nothing has been launched for it yet).
    token_path = github_runner_config.ssm_token_path
    try:
        runners = fleet.list_ec2_runners(ec2_runner_config.environment, runner_owner)
        launched_recently = launches.recent(token_path, runner_owner)
    except Exception as error:  # noqa: BLE001
        logger.warn("Could not count current runners; returning the group to SQS", owner=runner_owner, error=str(error))
        for job_id in by_job:
            decide(job_id, RETRY)
        return
    # Instances launched in the last few seconds are not listed yet; count them from our records.
    listed_jobs = {str(runner.job_id) for runner in runners}
    unlisted = launched_recently - listed_jobs
    current_runners = len(runners) + len(unlisted)

    # A job of this batch may already have an instance: launched by an earlier delivery whose
    # configuration or cleanup failed, or whose acknowledgement was lost. Finish or drop it
    # instead of launching a second one.
    instances_by_job: dict = {}
    for runner in runners:
        if runner.job_id in by_job:
            instances_by_job.setdefault(runner.job_id, []).append(runner.instance_id)
    pending = []
    for job_id in by_job:
        instance_ids = instances_by_job.get(job_id)
        if not instance_ids:
            if str(job_id) in unlisted:
                decide(job_id, RETRY)  # launched moments ago and not listed yet; look again next delivery
            else:
                pending.append(job_id)
            continue
        states = {instance_id: is_configured(github_runner_config, instance_id) for instance_id in instance_ids}
        keep = next((i for i, configured in states.items() if configured), None)
        if keep is None and None in states.values():
            decide(job_id, RETRY)  # SSM cannot tell us; try again on the next delivery
            continue
        if keep is None and configure_instance(github_runner_config, instance_ids[0], entry.github_installation_client):
            keep = instance_ids[0]
        for instance_id in instance_ids:
            if instance_id != keep and discard_instance(instance_id):
                current_runners -= 1
        logger.info("Job already has a runner instance", jobId=job_id, instance=keep, configured=keep is not None)
        decide(job_id, CONFIGURED if keep is not None else RETRY)

    if not pending:
        return

    new_runners = (
        len(pending) if maximum_runners == -1 else max(0, min(len(pending), maximum_runners - current_runners))
    )
    logger.info("Current runners", currentRunners=current_runners, maximumRunners=maximum_runners, owner=runner_owner)
    skipped = pending[: len(pending) - new_runners]
    if skipped:
        logger.info("Not all runners will be created for this group, maximum number of runners reached.",
                    desiredNewRunners=len(pending))
        for job_id in skipped:
            decide(job_id, RETRY if ephemeral_enabled else CONFIGURED)
    launch = pending[len(skipped):]
    if not launch:
        return

    logger.info("Attempting to launch new runners", newRunners=len(launch))
    for job_id in launch:
        decide(job_id, RETRY)  # until the launch says otherwise
    recorded = []
    for job_id in launch:
        try:
            launches.record(token_path, runner_owner, job_id)
            recorded.append(job_id)
        except Exception as error:  # noqa: BLE001 - an unrecorded launch could break the next cap check
            logger.warn("Could not record launch; returning the job to SQS", jobId=job_id, error=str(error))
    launch = recorded
    if not launch:
        return
    result = create_runners(github_runner_config, ec2_runner_config, launch, entry.github_installation_client)
    for job_id in result.configured:
        decide(job_id, CONFIGURED)
    for job_id in result.dropped:
        decide(job_id, DROPPED)
    logger.info("Runner creation summary.", requestedMessageCount=len(launch), configured=result.configured,
                retry=result.retry, dropped=result.dropped)
    if result.dropped:
        logger.warn("Some runner creation messages will not be retried through the current SQS batch.",
                    nonRetryableErrorCount=len(result.dropped))


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
