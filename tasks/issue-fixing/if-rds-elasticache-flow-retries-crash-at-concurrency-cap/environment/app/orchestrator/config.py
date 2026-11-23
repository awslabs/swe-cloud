"""Runtime settings, read from the environment.

Defaults match a production install; tests and local experiments usually scale the timings down.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _float(name: str, default: float) -> float:
    value = os.environ.get(name)
    return float(value) if value not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    pg_dsn: str
    valkey_url: str
    port: int
    # TTL given to a deployment concurrency lease when its slot is acquired (and the floor the
    # server keeps it alive for); clients renew it with their own lease_duration.
    lease_ttl_s: float
    # How far ahead an AwaitingConcurrencySlot state is scheduled when a deployment is full.
    slot_wait_s: float
    # Interval between repossessor scans of the expirations index.
    repossess_loop_s: float


def load() -> Settings:
    return Settings(
        pg_dsn=os.environ.get("PG_DSN", ""),
        valkey_url=os.environ.get("VALKEY_URL", "redis://localhost:6379/0"),
        port=int(os.environ.get("ORCH_PORT", "4200")),
        lease_ttl_s=_float("LEASE_TTL_S", 300.0),
        slot_wait_s=_float("SLOT_WAIT_S", 30.0),
        repossess_loop_s=_float("REPOSSESS_LOOP_S", 15.0),
    )
