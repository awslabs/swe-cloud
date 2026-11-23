"""The checkpointed handle of an in-progress part file."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class S3Recoverable:
    key: str                          # object key of the part file being written
    upload_id: str                    # its multipart upload
    parts: List[dict] = field(default_factory=list)   # [{"PartNumber", "ETag", "Size"}], all complete
    bytes_in_parts: int = 0
    tail_key: Optional[str] = None    # independent object holding the bytes not yet in a part
    tail_len: int = 0
    offset: int = 0                   # records persisted, for the source to resume from

    def to_json(self) -> str:
        return json.dumps({
            "key": self.key,
            "upload_id": self.upload_id,
            "parts": self.parts,
            "bytes_in_parts": self.bytes_in_parts,
            "tail_key": self.tail_key,
            "tail_len": self.tail_len,
            "offset": self.offset,
        }, separators=(",", ":"))

    @classmethod
    def from_dict(cls, d: dict) -> "S3Recoverable":
        return cls(
            key=d["key"],
            upload_id=d["upload_id"],
            parts=[dict(p) for p in d.get("parts", [])],
            bytes_in_parts=int(d.get("bytes_in_parts", 0)),
            tail_key=d.get("tail_key"),
            tail_len=int(d.get("tail_len", 0)),
            offset=int(d.get("offset", 0)),
        )
