"""The lock object's body and the version of it a writer last saw."""
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class LockData:
    expired: bool
    valid_until_ms: int
    owner: str

    def to_bytes(self) -> bytes:
        return json.dumps(
            {"expired": self.expired, "validUntil": self.valid_until_ms, "owner": self.owner}
        ).encode()

    @staticmethod
    def from_bytes(raw: bytes) -> "LockData":
        doc = json.loads(raw)
        return LockData(
            expired=bool(doc["expired"]),
            valid_until_ms=int(doc["validUntil"]),
            owner=str(doc["owner"]),
        )


@dataclass(frozen=True)
class LockFile:
    """A lock body together with the ETag storage returned for it."""

    data: LockData
    etag: str

    @property
    def owner(self) -> str:
        return self.data.owner

    @property
    def expired(self) -> bool:
        return self.data.expired

    @property
    def valid_until_ms(self) -> int:
        return self.data.valid_until_ms
