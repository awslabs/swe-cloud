"""Runtime settings, read from the environment the deployment provides."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    worker_id: str
    amqp_url: str
    work_queue: str
    fleet_table: str
    prefetch: int = 1


def from_env(worker_id: str) -> Settings:
    return Settings(
        worker_id=worker_id,
        amqp_url=os.environ["AMQP_URL"],
        work_queue=os.environ.get("WORK_QUEUE", "vm.destroy"),
        fleet_table=os.environ["FLEET_TABLE"],
    )
