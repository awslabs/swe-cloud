"""Concurrency lease storage on Valkey, in the prefect-redis layout.

    prefect:concurrency:lease:<id>      JSON lease record
    prefect:concurrency:expirations     sorted set: member <id>, score = expiration (epoch seconds)

Create, renew and revoke are Lua scripts so the record and its index entry change together.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import redis

logger = logging.getLogger("orchestrator.leases")

BASE_PREFIX = "prefect:concurrency:"
LEASE_PREFIX = BASE_PREFIX + "lease:"
EXPIRATIONS_KEY = BASE_PREFIX + "expirations"

_CREATE = """
-- KEYS[1] = lease_key, KEYS[2] = expirations_key
-- ARGV[1] = lease_json, ARGV[2] = expiration_ts, ARGV[3] = lease_id
redis.call('SET', KEYS[1], ARGV[1])
redis.call('ZADD', KEYS[2], ARGV[2], ARGV[3])
return 1
"""

_RENEW = """
local lease_key = KEYS[1]
local expirations_key = KEYS[2]
local lease_id = ARGV[1]
local new_expiration_timestamp = tonumber(ARGV[2])
local new_expiration_iso = ARGV[3]

local serialized_lease = redis.call('get', lease_key)
if not serialized_lease then
    -- Lease doesn't exist - clean up any orphaned index entry
    redis.call('zrem', expirations_key, lease_id)
    return 0
end

local lease_data = cjson.decode(serialized_lease)
lease_data['expiration'] = new_expiration_iso
redis.call('set', lease_key, cjson.encode(lease_data))
redis.call('zadd', expirations_key, new_expiration_timestamp, lease_id)
return 1
"""

_REVOKE = """
-- KEYS[1] = lease_key, KEYS[2] = expirations_key, ARGV[1] = lease_id
redis.call('DEL', KEYS[1])
redis.call('ZREM', KEYS[2], ARGV[1])
return 1
"""


@dataclass
class Lease:
    resource_ids: list[uuid.UUID]
    expiration: datetime
    slots: int = 1
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_json(self) -> str:
        return json.dumps(
            {
                "id": str(self.id),
                "resource_ids": [str(r) for r in self.resource_ids],
                "expiration": self.expiration.isoformat(),
                "created_at": self.created_at.isoformat(),
                "metadata": {"slots": self.slots},
            }
        )

    @classmethod
    def from_json(cls, data: str) -> "Lease":
        raw = json.loads(data)
        metadata = raw.get("metadata") or {}
        return cls(
            id=uuid.UUID(raw["id"]),
            resource_ids=[uuid.UUID(r) for r in raw["resource_ids"]],
            expiration=datetime.fromisoformat(raw["expiration"]),
            created_at=datetime.fromisoformat(raw["created_at"]),
            slots=int(metadata.get("slots", 1)),
        )


class LeaseStorage:
    def __init__(self, url: str):
        self.client = redis.Redis.from_url(
            url, decode_responses=True, socket_timeout=5, health_check_interval=10
        )
        self._create = self.client.register_script(_CREATE)
        self._renew = self.client.register_script(_RENEW)
        self._revoke = self.client.register_script(_REVOKE)

    @staticmethod
    def lease_key(lease_id: uuid.UUID | str) -> str:
        return f"{LEASE_PREFIX}{lease_id}"

    def ping(self) -> bool:
        return bool(self.client.ping())

    def create_lease(self, resource_ids: list[uuid.UUID], ttl: timedelta, slots: int = 1) -> Lease:
        lease = Lease(resource_ids=list(resource_ids), slots=slots,
                      expiration=datetime.now(timezone.utc) + ttl)
        self._create(
            keys=[self.lease_key(lease.id), EXPIRATIONS_KEY],
            args=[lease.to_json(), str(lease.expiration.timestamp()), str(lease.id)],
        )
        return lease

    def read_lease(self, lease_id: uuid.UUID | str) -> Lease | None:
        data = self.client.get(self.lease_key(lease_id))
        return Lease.from_json(data) if data is not None else None

    def renew_lease(self, lease_id: uuid.UUID | str, ttl: timedelta) -> bool:
        expiration = datetime.now(timezone.utc) + ttl
        result = self._renew(
            keys=[self.lease_key(lease_id), EXPIRATIONS_KEY],
            args=[str(lease_id), str(expiration.timestamp()), expiration.isoformat()],
        )
        return bool(result)

    def revoke_lease(self, lease_id: uuid.UUID | str) -> None:
        self._revoke(keys=[self.lease_key(lease_id), EXPIRATIONS_KEY], args=[str(lease_id)])

    def read_expired_lease_ids(self, limit: int = 100) -> list[uuid.UUID]:
        now = datetime.now(timezone.utc).timestamp()
        ids = self.client.zrangebyscore(EXPIRATIONS_KEY, "-inf", now, start=0, num=limit)
        return [uuid.UUID(i) for i in ids]

    def read_active_lease_ids(self, limit: int = 100) -> list[uuid.UUID]:
        now = datetime.now(timezone.utc).timestamp()
        ids = self.client.zrangebyscore(EXPIRATIONS_KEY, now, "+inf", start=0, num=limit)
        return [uuid.UUID(i) for i in ids]
