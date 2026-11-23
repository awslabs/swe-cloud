"""Redlock over a list of Valkey clients (a port of @sesamecare-oss/redlock 1.4.0).

A lock is acquired when a quorum of clients accept the acquire script. Any error a client raises
(refused connection, timeout, ...) is recorded as a vote against. With ``retry_count`` attempts
exhausted and no quorum, ``acquire`` (and ``extend``) raises ``ExecutionError``. A held lock can be
released, or extended by a new duration while it is still valid.
"""

from __future__ import annotations

import random
import secrets
import time

ACQUIRE_SCRIPT = """
-- Return 0 if an entry already exists.
for i, key in ipairs(KEYS) do
  if redis.call("exists", key) == 1 then
    return 0
  end
end

-- Create an entry for each provided key.
for i, key in ipairs(KEYS) do
  redis.call("set", key, ARGV[1], "PX", ARGV[2])
end

-- Return the number of entries added.
return #KEYS
"""

EXTEND_SCRIPT = """
-- Return 0 if an entry exists with a *different* lock value.
for i, key in ipairs(KEYS) do
  if redis.call("get", key) ~= ARGV[1] then
    return 0
  end
end

-- Update the entry for each provided key.
for i, key in ipairs(KEYS) do
  redis.call("set", key, ARGV[1], "PX", ARGV[2])
end

-- Return the number of entries updated.
return #KEYS
"""

RELEASE_SCRIPT = """
local count = 0
for i, key in ipairs(KEYS) do
  -- Only remove entries for *this* lock value.
  if redis.call("get", key) == ARGV[1] then
    redis.pcall("del", key)
    count = count + 1
  end
end

-- Return the number of entries removed.
return count
"""

QUORUM_MESSAGE = "The operation was unable to achieve a quorum during its retry window."


class ExecutionError(Exception):
    def __init__(self, message: str, attempts: list[dict]):
        super().__init__(message)
        self.attempts = attempts


class Lock:
    def __init__(self, redlock: "Redlock", resources: list[str], value: str, attempts: list[dict], expiration: float):
        self.redlock = redlock
        self.resources = resources
        self.value = value
        self.attempts = attempts
        self.expiration = expiration  # time.time() seconds

    def release(self) -> None:
        self.redlock.release(self)

    def extend(self, duration_ms: int) -> "Lock":
        return self.redlock.extend(self, duration_ms)


class Redlock:
    def __init__(self, clients, drift_factor: float = 0.01, retry_count: int = 10, retry_delay_ms: int = 200, retry_jitter_ms: int = 100):
        self.clients = list(clients)
        self.drift_factor = drift_factor
        self.retry_count = retry_count
        self.retry_delay_ms = retry_delay_ms
        self.retry_jitter_ms = retry_jitter_ms

    def _attempt(self, script: str, keys: list[str], args: list) -> tuple[bool, dict]:
        votes_for, votes_against = [], {}
        for client in self.clients:
            try:
                result = client.eval(script, len(keys), *keys, *args)
                if int(result) == len(keys):
                    votes_for.append(client)
                else:
                    votes_against[client] = f"The operation was applied to: {result} of the {len(keys)} requested resources."
            except Exception as error:  # any client error is a vote against
                votes_against[client] = repr(error)
        quorum = len(self.clients) // 2 + 1
        return len(votes_for) >= quorum, {"quorum": quorum, "for": len(votes_for), "against": list(votes_against.values())}

    def _execute(self, script: str, keys: list[str], args: list) -> tuple[float, list[dict]]:
        attempts: list[dict] = []
        while True:
            start = time.time()
            ok, stats = self._attempt(script, keys, args)
            attempts.append(stats)
            if ok:
                return start, attempts
            if len(attempts) > self.retry_count:
                raise ExecutionError(QUORUM_MESSAGE, attempts)
            time.sleep(max(0, self.retry_delay_ms + random.uniform(-1, 1) * self.retry_jitter_ms) / 1000)

    def acquire(self, resources: list[str], duration_ms: int) -> Lock:
        value = secrets.token_hex(16)
        try:
            start, attempts = self._execute(ACQUIRE_SCRIPT, resources, [value, int(duration_ms)])
        except ExecutionError:
            # Release any partial lock state that may exist on a minority of clients.
            self._attempt(RELEASE_SCRIPT, resources, [value])
            raise
        # Add 2 milliseconds to the drift to account for Redis expires precision,
        # which is 1 ms, plus the configured allowable drift factor.
        drift = round(self.drift_factor * duration_ms) + 2
        return Lock(self, resources, value, attempts, start + (duration_ms - drift) / 1000)

    def release(self, lock: Lock) -> None:
        self._execute(RELEASE_SCRIPT, lock.resources, [lock.value])

    def extend(self, existing: Lock, duration_ms: int) -> Lock:
        """Extend a valid lock by ``duration_ms``; the existing ``Lock`` is invalidated."""
        # The lock has already expired.
        if existing.expiration < time.time():
            raise ExecutionError("Cannot extend an already-expired lock.", [])
        start, attempts = self._execute(EXTEND_SCRIPT, existing.resources, [existing.value, int(duration_ms)])
        # Invalidate the existing lock.
        existing.expiration = 0
        drift = round(self.drift_factor * duration_ms) + 2
        return Lock(self, existing.resources, existing.value, attempts, start + (duration_ms - drift) / 1000)
