"""Runtime configuration, read from the environment.

Every process (worker or callback server) is configured the same way. The queue URLs and the
table name are injected by the deployment; the defaults below match tools/bootstrap.py so the
local stack works without exporting anything.
"""
import os
from functools import lru_cache

import boto3

SUBMISSION_QUEUE_NAME = "submission-queue"
REPROCESS_QUEUE_NAME = "reprocess-queue"
DLQ_NAME = "submission-dlq"

TABLE_NAME = os.environ.get("TABLE_NAME", "ReliabilityQueue")
NOTIFY_API_URL = os.environ.get("NOTIFY_API_URL", "http://127.0.0.1:8025").rstrip("/")
NOTIFY_API_KEY = os.environ.get("NOTIFY_API_KEY", "gcforms-local-key")
NOTIFY_TEMPLATE_ID = os.environ.get("NOTIFY_TEMPLATE_ID", "8b6c1d1e-6f1a-4a0e-9d5e-2c3f1b7a9e10")
NOTIFY_CALLBACK_TOKEN = os.environ.get("NOTIFY_CALLBACK_TOKEN", "gcforms-local-callback-token")

# The event source mapping in production used batch size 1; workers keep that.
POLL_WAIT_S = 1
VISIBILITY_TIMEOUT_S = int(os.environ.get("VISIBILITY_TIMEOUT_S", "12"))


@lru_cache(maxsize=None)
def sqs():
    return boto3.client("sqs")


@lru_cache(maxsize=None)
def dynamodb():
    return boto3.client("dynamodb")


def _queue_url(env_name, queue_name):
    url = os.environ.get(env_name)
    if url:
        return url
    return sqs().get_queue_url(QueueName=queue_name)["QueueUrl"]


@lru_cache(maxsize=None)
def submission_queue_url():
    return _queue_url("SUBMISSION_QUEUE_URL", SUBMISSION_QUEUE_NAME)


@lru_cache(maxsize=None)
def reprocess_queue_url():
    return _queue_url("REPROCESS_QUEUE_URL", REPROCESS_QUEUE_NAME)
