"""Pool configuration, read from the environment (see README.md)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _float(name: str, default: float) -> float:
    value = os.environ.get(name)
    return float(value) if value not in (None, "") else default


def _int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return int(value) if value not in (None, "") else default


@dataclass(frozen=True)
class Config:
    valkey_host: str = field(default_factory=lambda: os.environ.get("VALKEY_HOST", "localhost"))
    valkey_port: int = field(default_factory=lambda: _int("VALKEY_PORT", 6379))
    namespace: str = field(default_factory=lambda: os.environ.get("NAMESPACE", "work"))
    job_type: str = field(default_factory=lambda: os.environ.get("JOB_TYPE", "process"))
    max_concurrency: int = field(default_factory=lambda: _int("MAX_CONCURRENCY", 0))
    heartbeat_period_s: float = field(default_factory=lambda: _float("HEARTBEAT_PERIOD_S", 5.0))
    dead_time_s: float = field(default_factory=lambda: _float("DEAD_TIME_S", 10.0))
    reap_period_s: float = field(default_factory=lambda: _float("REAP_PERIOD_S", 600.0))
    reap_jitter_s: int = field(default_factory=lambda: _int("REAP_JITTER_S", 30))
    jobrun: str = field(default_factory=lambda: os.environ.get("JOBRUN", "/app/bin/jobrun"))
