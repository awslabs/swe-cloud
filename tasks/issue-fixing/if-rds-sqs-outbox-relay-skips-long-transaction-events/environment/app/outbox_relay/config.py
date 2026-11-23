from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "relay.toml")


@dataclass(frozen=True)
class RelayConfig:
    poll_interval_seconds: float
    batch_limit: int
    group_id: str
    stale_gap_seconds: float
    queue_url: str


def load(path: str = DEFAULT_PATH) -> RelayConfig:
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    relay, gaps = raw.get("relay", {}), raw.get("gaps", {})
    return RelayConfig(
        poll_interval_seconds=float(relay.get("poll_interval_seconds", 0.25)),
        batch_limit=int(relay.get("batch_limit", 500)),
        group_id=str(relay.get("group_id", "outbox")),
        stale_gap_seconds=float(gaps.get("stale_gap_seconds", 10)),
        queue_url=os.environ["OUTBOX_QUEUE_URL"],
    )
