"""Flow-run state types and the state value carried through orchestration."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

SCHEDULED = "SCHEDULED"
PENDING = "PENDING"
RUNNING = "RUNNING"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
CRASHED = "CRASHED"
CANCELLING = "CANCELLING"
CANCELLED = "CANCELLED"

ALL_TYPES = {SCHEDULED, PENDING, RUNNING, COMPLETED, FAILED, CRASHED, CANCELLING, CANCELLED}
TERMINAL_TYPES = {COMPLETED, FAILED, CRASHED, CANCELLED}

DEFAULT_NAMES = {
    SCHEDULED: "Scheduled",
    PENDING: "Pending",
    RUNNING: "Running",
    COMPLETED: "Completed",
    FAILED: "Failed",
    CRASHED: "Crashed",
    CANCELLING: "Cancelling",
    CANCELLED: "Cancelled",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class State:
    type: str
    name: str
    # state_details.deployment_concurrency_lease_id
    lease_id: uuid.UUID | None = None
    scheduled_time: datetime | None = None
    message: str | None = None

    def copy(self, **changes: Any) -> "State":
        return replace(self, **changes)

    def to_json(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "name": self.name,
            "lease_id": str(self.lease_id) if self.lease_id else None,
            "scheduled_time": self.scheduled_time.isoformat() if self.scheduled_time else None,
            "message": self.message,
        }

    @classmethod
    def from_request(cls, body: dict[str, Any]) -> "State":
        state_type = str(body.get("type", "")).upper()
        if state_type not in ALL_TYPES:
            raise ValueError(f"unknown state type {body.get('type')!r}")
        scheduled = body.get("scheduled_time")
        lease = body.get("lease_id")
        return cls(
            type=state_type,
            name=body.get("name") or DEFAULT_NAMES[state_type],
            lease_id=uuid.UUID(lease) if lease else None,
            scheduled_time=datetime.fromisoformat(scheduled) if scheduled else None,
            message=body.get("message"),
        )
