"""Lock body and the version a writer last saw. The body keeps any extra keys a writer stores."""
import json
from dataclasses import dataclass, field


@dataclass(frozen=True)
class LockData:
    expired: bool
    valid_until_ms: int
    owner: str
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        doc = {"expired": self.expired, "validUntil": self.valid_until_ms, "owner": self.owner}
        doc.update(self.extra)
        return doc

    def to_bytes(self) -> bytes:
        return json.dumps(self.to_dict()).encode()

    @staticmethod
    def from_dict(doc: dict) -> "LockData":
        extra = {k: v for k, v in doc.items() if k not in ("expired", "validUntil", "owner")}
        return LockData(expired=bool(doc["expired"]), valid_until_ms=int(doc["validUntil"]),
                        owner=str(doc["owner"]), extra=extra)

    @staticmethod
    def from_bytes(raw: bytes) -> "LockData":
        return LockData.from_dict(json.loads(raw))


@dataclass(frozen=True)
class LockFile:
    data: LockData
    etag: str

    @property
    def owner(self):
        return self.data.owner

    @property
    def expired(self):
        return self.data.expired

    @property
    def valid_until_ms(self):
        return self.data.valid_until_ms

    @property
    def acq_id(self):
        return self.data.extra.get("acqId")
