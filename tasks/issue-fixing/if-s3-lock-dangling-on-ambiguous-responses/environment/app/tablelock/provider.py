"""Table lock provider over a single S3 lock object.

A lock object is held while ``expired`` is false and ``now <= validUntil + CLOCK_DRIFT_BUFFER_MS``.
Acquire overwrites an expired or lapsed version, release rewrites the held version with
``expired=true`` and the same ``validUntil``. See docs/LOCKING.md.

One provider instance belongs to one process. Holding is tracked per instance: ``try_lock`` on an
instance that already holds the lock returns True straight away.
"""
import enum
import logging
import time

from .lockfile import LockData, LockFile
from .storage import GetResult, S3LockClient, UpsertResult

log = logging.getLogger("tablelock.provider")

# Maximum expected clock drift between two writers.
CLOCK_DRIFT_BUFFER_MS = 500

# Retries of the release write after a THROTTLED answer, with 1s, 2s, 4s backoff.
THROTTLE_MAX_RETRIES = 3
THROTTLE_INITIAL_RETRY_DELAY_SECONDS = 1

CAUSE_THROTTLE_RETRIES_EXHAUSTED = "THROTTLE_RETRIES_EXHAUSTED"
CAUSE_EXPIRE_WRITE_FAILED = "EXPIRE_WRITE_FAILED"


class LockState(enum.Enum):
    ACQUIRING = "ACQUIRING"
    ACQUIRED = "ACQUIRED"
    FAILED_TO_ACQUIRE = "FAILED_TO_ACQUIRE"
    RELEASING = "RELEASING"
    RELEASED = "RELEASED"
    FAILED_TO_RELEASE = "FAILED_TO_RELEASE"


class ExpireResult(enum.Enum):
    SUCCESS = "SUCCESS"
    THROTTLED = "THROTTLED"
    FAILED = "FAILED"


class LockReleaseError(Exception):
    pass


def now_ms() -> int:
    return int(time.time() * 1000)


class LockProvider:
    def __init__(self, client: S3LockClient, owner: str, lease_seconds: int):
        # The writer name this process locks under (--owner). It names the job, not the process.
        self.owner = owner
        self.client = client
        self.lease_seconds = lease_seconds
        self._current: LockFile | None = None

    # ----------------------------------------------------------------- state

    @property
    def current(self) -> LockFile | None:
        return self._current

    def _set_lock(self, lock: LockFile | None):
        if lock is not None and lock.owner != self.owner:
            raise RuntimeError(f"Owners do not match: {self.owner} vs {lock.owner}")
        self._current = lock

    def _state(self, state: LockState, msg: str = "") -> str:
        text = f"Owner {self.owner}: Lock {self.client.key}, state {state.value}"
        return f"{text}, {msg}" if msg else text

    def _lock_expiration(self, at_ms: int) -> int:
        # Lease ends are kept on whole seconds: the lock inspector and the dangling-lock alert
        # compare them with second-resolution log timestamps.
        return (at_ms // 1000 + self.lease_seconds) * 1000

    @staticmethod
    def _certainly_past(epoch_ms: int) -> bool:
        return now_ms() > epoch_ms + CLOCK_DRIFT_BUFFER_MS

    def _is_still_valid(self, lock: LockFile) -> bool:
        if lock.expired:
            return False
        if self._certainly_past(lock.valid_until_ms):
            log.warning(self._state(LockState.ACQUIRING,
                                    f"Found a dangling expired lock with owner {lock.owner}"))
            return False
        return True

    def _actually_holds_lock(self) -> bool:
        return self._current is not None and self._is_still_valid(self._current)

    # --------------------------------------------------------------- acquire

    def try_lock(self) -> bool:
        """One non-blocking attempt to acquire the lock."""
        if self._actually_holds_lock():
            return True

        result, latest = self.client.read_current()
        if result is GetResult.UNKNOWN_ERROR:
            log.info(self._state(LockState.FAILED_TO_ACQUIRE, "Failed to get the latest lock status"))
            return False
        if result is GetResult.NOT_EXISTS:
            raise RuntimeError(f"lock object {self.client.key} is missing; run bootstrap.py")
        if self._is_still_valid(latest):
            log.info(self._state(LockState.FAILED_TO_ACQUIRE, f"Lock already held by {latest.owner}"))
            return False

        new_data = LockData(expired=False, valid_until_ms=self._lock_expiration(now_ms()),
                            owner=self.owner)
        status, written = self.client.try_upsert(new_data, latest)
        if status is not UpsertResult.SUCCESS:
            log.info(self._state(LockState.FAILED_TO_ACQUIRE, status.value))
            return False
        self._set_lock(written)
        log.info(self._state(LockState.ACQUIRED, f"etag {written.etag}"))
        return True

    def try_lock_for(self, timeout_s: float, poll_ms: int) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.try_lock():
                return True
            time.sleep(poll_ms / 1000.0)
        log.info(self._state(LockState.FAILED_TO_ACQUIRE, f"gave up after {timeout_s}s"))
        return False

    # --------------------------------------------------------------- release

    def unlock(self):
        """Release by rewriting the held version with expired=true. Raises LockReleaseError."""
        if self._current is None:
            return
        lock_to_expire = self._current
        result = self._try_expire()

        attempt = 1
        while attempt <= THROTTLE_MAX_RETRIES and result is ExpireResult.THROTTLED:
            delay = THROTTLE_INITIAL_RETRY_DELAY_SECONDS << (attempt - 1)
            log.warning("Owner %s: lock expiration was throttled (retry %d/%d), backing off %ds",
                        self.owner, attempt, THROTTLE_MAX_RETRIES, delay)
            time.sleep(delay)
            if self._current is not lock_to_expire:
                return
            result = self._try_expire()
            attempt += 1

        if result is not ExpireResult.SUCCESS:
            cause = (CAUSE_THROTTLE_RETRIES_EXHAUSTED if result is ExpireResult.THROTTLED
                     else CAUSE_EXPIRE_WRITE_FAILED)
            log.error("Owner %s: cannot release lock %s - expire write ended as %s (cause=%s); "
                      "lock left un-expired and will dangle until its lease elapses",
                      self.owner, self.client.key, result.value, cause)
            raise LockReleaseError(self._state(LockState.FAILED_TO_RELEASE, f"cause {cause}"))

    def _try_expire(self) -> ExpireResult:
        """A single attempt to expire the held lock; callers own the retry policy."""
        held = self._current
        expired_data = LockData(expired=True, valid_until_ms=held.valid_until_ms, owner=self.owner)
        status, _ = self.client.try_upsert(expired_data, held)
        if status is UpsertResult.UNKNOWN_ERROR:
            log.error(self._state(LockState.FAILED_TO_RELEASE, "Lock state is unknown."))
            return ExpireResult.FAILED
        if status is UpsertResult.THROTTLED:
            log.warning(self._state(LockState.FAILED_TO_RELEASE, "Lock expiration write was throttled."))
            return ExpireResult.THROTTLED
        if status is UpsertResult.SUCCESS:
            log.info(self._state(LockState.RELEASED))
            self._set_lock(None)
            return ExpireResult.SUCCESS
        # ACQUIRED_BY_OTHERS
        log.error("Owner %s: lock %s was acquired by another owner before we could expire it; "
                  "our lease validUntil was %d ms ago", self.owner, self.client.key,
                  now_ms() - held.valid_until_ms)
        log.error(self._state(LockState.FAILED_TO_RELEASE, "lock was acquired by others."))
        self._set_lock(None)
        return ExpireResult.FAILED
