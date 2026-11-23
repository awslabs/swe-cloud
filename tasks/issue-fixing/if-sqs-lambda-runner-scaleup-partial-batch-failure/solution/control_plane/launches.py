"""Recent launches per owner, kept in Parameter Store under SSM_TOKEN_PATH.

describe-runners is DescribeInstances underneath and lags launches by up to 8 s (README, "Fleet
API"), so the cap check would not see instances this function launched a moment ago, in this
invocation or the one before. A launch is recorded before CreateFleet is called; the cap check counts
every recent record whose job describe-runners does not list yet.
"""

from __future__ import annotations

import json
import time

from . import ssm
from .logger import create_child_logger

logger = create_child_logger("launches")

WINDOW_S = 10.0  # the documented 8 s lag, with a margin
MAX_PRUNE = 5


def _path(token_path: str, owner: str) -> str:
    return f"{token_path}/_launches/{owner}"


def recent(token_path: str, owner: str) -> set[str]:
    """Job ids launched for `owner` within WINDOW_S. Raises when Parameter Store cannot tell."""
    now = time.time()
    jobs: set[str] = set()
    stale = []
    for name, value in ssm.parameters_by_path(_path(token_path, owner)).items():
        try:
            record = json.loads(value)
            at, job_id = float(record["at"]), str(record["job_id"])
        except (ValueError, KeyError, TypeError):
            stale.append(name)
            continue
        if now - at < WINDOW_S:
            jobs.add(job_id)
        else:
            stale.append(name)
    for name in stale[:MAX_PRUNE]:
        try:
            ssm.delete_parameter(name)
        except Exception as error:  # noqa: BLE001 - pruning is housekeeping
            logger.debug("Could not prune launch record", name=name, error=str(error))
    return jobs


def record(token_path: str, owner: str, job_id) -> None:
    ssm.put_parameter(
        f"{_path(token_path, owner)}/{job_id}", json.dumps({"job_id": job_id, "at": time.time()}), False, overwrite=True
    )
