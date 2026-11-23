"""A job as it travels through Valkey: the raw JSON is kept byte for byte, since LREM matches on it."""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass
class Job:
    name: str
    id: str
    enqueued_at: int
    args: dict
    raw_json: bytes
    dequeued_from: bytes
    in_prog_queue: bytes

    @classmethod
    def parse(cls, raw_json: bytes, dequeued_from: bytes, in_prog_queue: bytes) -> "Job":
        body = json.loads(raw_json)
        return cls(
            name=body["name"],
            id=body["id"],
            enqueued_at=int(body.get("t", 0)),
            args=body.get("args") or {},
            raw_json=raw_json,
            dequeued_from=dequeued_from,
            in_prog_queue=in_prog_queue,
        )
