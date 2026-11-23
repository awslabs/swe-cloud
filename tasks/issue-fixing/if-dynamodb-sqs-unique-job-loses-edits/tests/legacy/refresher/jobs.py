"""The queued job and its wire format."""

import json
import time
from dataclasses import dataclass, field

JOB_CLASS = "RefreshEntity"


@dataclass
class RefreshEntity:
    entity_id: str
    unique_for: int
    dispatched_at: float = field(default_factory=time.time)

    def unique_key(self):
        return f"refresh_unique:{JOB_CLASS}:{self.entity_id}"

    def overlap_key(self):
        return f"refresh_overlap:{JOB_CLASS}:{self.entity_id}"

    def to_body(self):
        return json.dumps({
            "job": JOB_CLASS,
            "entity_id": self.entity_id,
            "unique_for": self.unique_for,
            "dispatched_at": self.dispatched_at,
        })

    @classmethod
    def from_body(cls, body):
        data = json.loads(body)
        if data.get("job") != JOB_CLASS:
            raise ValueError(f"unknown job class {data.get('job')!r}")
        return cls(entity_id=data["entity_id"], unique_for=int(data["unique_for"]),
                   dispatched_at=float(data["dispatched_at"]))
