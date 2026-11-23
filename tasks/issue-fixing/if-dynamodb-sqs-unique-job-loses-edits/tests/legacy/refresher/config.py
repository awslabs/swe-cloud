"""Runtime configuration, read from the environment on every call."""

import os
from dataclasses import dataclass

import boto3


def _int(name, default):
    value = os.environ.get(name)
    return default if value in (None, "") else int(value)


@dataclass(frozen=True)
class Config:
    queue_url: str
    unique_table: str
    overlap_table: str
    entity_table: str
    unique_for: int
    release_after: int
    overlap_expires: int
    backoff: int
    max_tries: int
    receive_wait: int
    refresh_cmd: str


def load():
    queue_url = os.environ.get("REFRESH_QUEUE_URL", "")
    if not queue_url:
        queue_url = sqs().get_queue_url(
            QueueName=os.environ.get("REFRESH_QUEUE_NAME", "refresh-jobs"))["QueueUrl"]
    return Config(
        queue_url=queue_url,
        unique_table=os.environ.get("UNIQUE_LOCK_TABLE", "refresh_unique_locks"),
        overlap_table=os.environ.get("OVERLAP_LOCK_TABLE", "refresh_overlap_locks"),
        entity_table=os.environ.get("ENTITY_TABLE", "catalog_entities"),
        unique_for=_int("UNIQUE_FOR", 600),
        release_after=_int("RELEASE_AFTER", 5),
        overlap_expires=_int("OVERLAP_EXPIRES", 120),
        backoff=_int("BACKOFF", 2),
        max_tries=_int("MAX_TRIES", 10),
        receive_wait=_int("RECEIVE_WAIT", 1),
        refresh_cmd=os.environ.get("REFRESH_CMD", "python3 -m refresher.recompute"),
    )


def dynamodb():
    return boto3.client("dynamodb")


def sqs():
    return boto3.client("sqs")
