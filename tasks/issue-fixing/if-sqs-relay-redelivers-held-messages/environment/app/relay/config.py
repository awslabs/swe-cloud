"""Relay configuration, read from the environment (see README.md)."""

import os
from dataclasses import dataclass


def _int(name, default):
    value = os.environ.get(name)
    return default if value in (None, "") else int(value)


def _bool(name, default):
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class RelayConfig:
    queue_url: str
    downstream_url: str
    relay_id: str
    endpoint_url: str | None
    region: str
    visibility_timeout: int
    max_messages_per_poll: int
    wait_time_seconds: int
    concurrent_request_limit: int
    extend_message_visibility: bool
    poll_delay_ms: int

    @classmethod
    def from_env(cls):
        queue_url = os.environ.get("QUEUE_URL")
        downstream_url = os.environ.get("DOWNSTREAM_URL")
        if not queue_url or not downstream_url:
            raise SystemExit("QUEUE_URL and DOWNSTREAM_URL must be set (see README.md)")
        return cls(
            queue_url=queue_url,
            downstream_url=downstream_url,
            relay_id=os.environ.get("RELAY_ID") or f"relay-{os.getpid()}",
            endpoint_url=os.environ.get("AWS_ENDPOINT_URL_SQS") or os.environ.get("AWS_ENDPOINT_URL") or None,
            region=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1",
            visibility_timeout=_int("VISIBILITY_TIMEOUT", 20),
            max_messages_per_poll=_int("MAX_MESSAGES_PER_POLL", 20),
            wait_time_seconds=_int("WAIT_TIME_SECONDS", 10),
            concurrent_request_limit=_int("CONCURRENT_REQUEST_LIMIT", 50),
            extend_message_visibility=_bool("EXTEND_MESSAGE_VISIBILITY", True),
            poll_delay_ms=_int("POLL_DELAY_MS", 500),
        )
