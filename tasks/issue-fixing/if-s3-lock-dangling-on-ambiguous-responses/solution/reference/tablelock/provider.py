"""Table lock provider that reconciles ambiguous conditional-write answers from the lock object.

The fix over the master behaviour:

* 5xx and dropped connections are ambiguous, not terminal. Acquire and release retry the identical
  conditional write (same If-Match) a bounded number of times.
* Every acquire stamps a fresh per-acquisition id (``acqId``) into the lock body. That id, not the
  owner, is what tells this acquisition's own landed write apart from anyone else's — including the
  compaction sidecar, which locks under the same owner and can land on the same whole-second lease.
* After an ambiguous attempt, a later 412 (or a lock that still looks held) is reconciled by
  reading the lock object back: adopt it only if it carries our acqId, is not expired and is still
  valid (acquire); count the release as landed only if it carries our acqId and is expired.
"""
import enum
import logging
import time
import uuid

from .lockfile import LockData, LockFile
from .storage import GetResult, S3LockClient, UpsertResult

log = logging.getLogger("tablelock.provider")

CLOCK_DRIFT_BUFFER_MS = 500
AMBIGUOUS_MAX_RETRIES = 4
AMBIGUOUS_BACKOFF_MS = 150
THROTTLE_MAX_RETRIES = 3
THROTTLE_INITIAL_RETRY_DELAY_SECONDS = 1


class LockReleaseError(Exception):
    pass


def now_ms():
    return int(time.time() * 1000)


class LockProvider:
    def __init__(self, client: S3LockClient, owner: str, lease_seconds: int):
        self.owner = owner
        self.client = client
        self.lease_seconds = lease_seconds
        self._current: LockFile | None = None

    @property
    def current(self):
        return self._current

    def _lock_expiration(self, at_ms):
        return (at_ms // 1000 + self.lease_seconds) * 1000

    @staticmethod
    def _certainly_past(epoch_ms):
        return now_ms() > epoch_ms + CLOCK_DRIFT_BUFFER_MS

    def _is_valid(self, lf: LockFile):
        return (not lf.expired) and (not self._certainly_past(lf.valid_until_ms))

    def _holds(self):
        return self._current is not None and self._is_valid(self._current)

    # ---- acquire -----------------------------------------------------------

    def try_lock(self):
        if self._holds():
            return True
        result, latest = self.client.read_current()
        if result is GetResult.UNKNOWN_ERROR:
            return False
        if result is GetResult.NOT_EXISTS:
            raise RuntimeError(f"lock object {self.client.key} missing; run bootstrap.py")
        if self._is_valid(latest):
            return False

        acq_id = uuid.uuid4().hex
        valid_until = self._lock_expiration(now_ms())
        data = LockData(expired=False, valid_until_ms=valid_until, owner=self.owner,
                        extra={"acqId": acq_id})
        status, written = self.client.try_upsert(data, latest)
        unresolved = False
        attempt = 0
        while status is UpsertResult.AMBIGUOUS and attempt < AMBIGUOUS_MAX_RETRIES:
            unresolved = True
            time.sleep(AMBIGUOUS_BACKOFF_MS / 1000.0)
            status, written = self.client.try_upsert(data, latest)
            attempt += 1

        if status is UpsertResult.SUCCESS:
            self._current = written
            return True
        if status is UpsertResult.ACQUIRED_BY_OTHERS and unresolved:
            # A 412 after an ambiguous attempt: our own earlier write may have landed.
            adopted = self._adopt_landed_acquire(acq_id, valid_until)
            if adopted is not None:
                self._current = adopted
                return True
        if status is UpsertResult.AMBIGUOUS:
            adopted = self._adopt_landed_acquire(acq_id, valid_until)
            if adopted is not None:
                self._current = adopted
                return True
        return False

    def _adopt_landed_acquire(self, acq_id, valid_until):
        result, lf = self.client.read_current()
        if result is not GetResult.SUCCESS or lf is None:
            return None
        if (lf.owner == self.owner and lf.acq_id == acq_id and not lf.expired
                and lf.valid_until_ms == valid_until and self._is_valid(lf)):
            return lf
        return None

    def try_lock_for(self, timeout_s, poll_ms):
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.try_lock():
                return True
            time.sleep(poll_ms / 1000.0)
        return False

    # ---- release -----------------------------------------------------------

    def unlock(self):
        if self._current is None:
            return
        held = self._current
        acq_id = held.acq_id
        expired = LockData(expired=True, valid_until_ms=held.valid_until_ms, owner=self.owner,
                           extra=dict(held.data.extra))
        status, _ = self.client.try_upsert(expired, held)

        unresolved = False
        attempt = 0
        while status in (UpsertResult.AMBIGUOUS, UpsertResult.THROTTLED) and attempt < AMBIGUOUS_MAX_RETRIES + THROTTLE_MAX_RETRIES:
            unresolved = unresolved or status is UpsertResult.AMBIGUOUS
            time.sleep(AMBIGUOUS_BACKOFF_MS / 1000.0)
            status, _ = self.client.try_upsert(expired, held)
            attempt += 1

        if status is UpsertResult.SUCCESS:
            self._current = None
            return
        if status is UpsertResult.ACQUIRED_BY_OTHERS and unresolved and self._release_landed(acq_id, held.valid_until_ms):
            self._current = None
            return
        if status is UpsertResult.AMBIGUOUS and self._release_landed(acq_id, held.valid_until_ms):
            self._current = None
            return
        self._current = None
        raise LockReleaseError(f"Owner {self.owner}: FAILED_TO_RELEASE {self.client.key}")

    def _release_landed(self, acq_id, valid_until):
        result, lf = self.client.read_current()
        if result is not GetResult.SUCCESS or lf is None:
            return False
        return (lf.owner == self.owner and lf.acq_id == acq_id and lf.expired
                and lf.valid_until_ms == valid_until)
