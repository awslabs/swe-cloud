"""Configuration, read from the environment the way the Helm chart passes it."""

import os
from dataclasses import dataclass

SQS_MSG_VISIBILITY_TIMEOUT_SEC_DEFAULT = 20
WORKERS_DEFAULT = 10
NODE_TERMINATION_GRACE_PERIOD_DEFAULT = 120


class ConfigError(Exception):
    pass


@dataclass
class Config:
    queue_url: str
    cluster_api_url: str
    cluster_api_token: str
    pod_name: str
    sqs_msg_visibility_timeout_sec: int = SQS_MSG_VISIBILITY_TIMEOUT_SEC_DEFAULT
    workers: int = WORKERS_DEFAULT
    node_termination_grace_period: int = NODE_TERMINATION_GRACE_PERIOD_DEFAULT
    check_tag_before_draining: bool = True
    delete_sqs_msg_if_node_not_found: bool = False
    log_level: str = "info"


def _int_env(name, default):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _bool_env(name, default):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _required(name):
    value = os.environ.get(name, "")
    if not value:
        raise ConfigError(f"{name} is required")
    return value


def load():
    config = Config(
        queue_url=_required("QUEUE_URL"),
        cluster_api_url=_required("CLUSTER_API_URL").rstrip("/"),
        cluster_api_token=_required("CLUSTER_API_TOKEN"),
        pod_name=os.environ.get("POD_NAME") or os.uname().nodename,
        sqs_msg_visibility_timeout_sec=_int_env(
            "SQS_MSG_VISIBILITY_TIMEOUT_SEC", SQS_MSG_VISIBILITY_TIMEOUT_SEC_DEFAULT),
        workers=_int_env("WORKERS", WORKERS_DEFAULT),
        node_termination_grace_period=_int_env(
            "NODE_TERMINATION_GRACE_PERIOD", NODE_TERMINATION_GRACE_PERIOD_DEFAULT),
        check_tag_before_draining=_bool_env("CHECK_TAG_BEFORE_DRAINING", True),
        delete_sqs_msg_if_node_not_found=_bool_env("DELETE_SQS_MSG_IF_NODE_NOT_FOUND", False),
        log_level=os.environ.get("LOG_LEVEL", "info"),
    )
    if config.sqs_msg_visibility_timeout_sec <= 0 or config.sqs_msg_visibility_timeout_sec >= 120:
        raise ConfigError(
            "invalid SqsMsgVisibilityTimeoutSec configuration: "
            "SqsMsgVisibilityTimeoutSec valid range from 1 to 119")
    if config.workers < 1:
        raise ConfigError("WORKERS must be at least 1")
    return config
