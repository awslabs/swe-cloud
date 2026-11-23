import os
from dataclasses import dataclass


@dataclass
class ReceiveSettings:
    queue_url: str
    endpoint_url: str
    region: str
    work_url: str
    concurrency: int
    wait_time_seconds: int
    redeliver_visibility_timeout: int
    work_timeout_seconds: float
    # Replaced at startup by the queue's own VisibilityTimeout attribute.
    visibility_timeout: int = 30

    @classmethod
    def from_env(cls):
        env = os.environ
        return cls(
            queue_url=env["QUEUE_URL"],
            endpoint_url=env.get("SQS_ENDPOINT_URL") or env.get("AWS_ENDPOINT_URL") or "http://localhost:4566",
            region=env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or "us-east-1",
            work_url=env["WORK_URL"],
            concurrency=int(env.get("CONCURRENCY", "4")),
            wait_time_seconds=int(env.get("RECEIVE_WAIT_TIME_SECONDS", "1")),
            redeliver_visibility_timeout=int(env.get("REDELIVER_VISIBILITY_TIMEOUT_SECONDS", "0")),
            work_timeout_seconds=float(env.get("WORK_TIMEOUT_SECONDS", "120")),
        )
