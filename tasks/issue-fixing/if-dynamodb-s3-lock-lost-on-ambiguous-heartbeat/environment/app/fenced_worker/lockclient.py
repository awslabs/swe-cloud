"""Lease lock on a single DynamoDB item.

Python port of the Go lease-lock client the platform team maintains for the
other services (same item layout, same heartbeat and takeover rules), so that
the Go and Python writers can share a lock table.

Item layout (hash key ``key``):

    key                  lock name
    ownerName            "<worker-id>:<nonce>", unique per process start
    recordVersionNumber  opaque version tag, rewritten by every heartbeat
    leaseDuration        lease in milliseconds (N)
    fence                epoch counter, +1 on every acquisition (N)

A holder renews its lease by rotating recordVersionNumber with a conditional
UpdateItem.  A client that wants the lock reads the item and, if the
recordVersionNumber it saw stays unchanged for leaseDuration (plus a buffer)
measured on its own clock, replaces the item with a PutItem conditioned on
that recordVersionNumber.  No absolute times are stored.
"""

import logging
import secrets
import threading
import time

from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

log = logging.getLogger("fenced_worker.lock")

_DEADLINE_ERRORS = (ReadTimeoutError, ConnectTimeoutError, EndpointConnectionError)


class LockNotGranted(Exception):
    """The lock is not (or no longer) held by this client."""


class HeartbeatDeadlineExceeded(Exception):
    """A heartbeat did not complete within its per-call deadline."""


def new_record_version_number():
    return "%d:%s" % (time.time_ns(), secrets.token_hex(16))


def _parse_dynamodb_error(err, msg):
    if isinstance(err, ClientError):
        if err.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return LockNotGranted("%s: %s" % (msg, err))
    return err


class Lock:
    def __init__(self, key, owner, rvn, fence, lease_s, lookup_time):
        self.semaphore = threading.Lock()
        self.key = key
        self.owner = owner
        self.rvn = rvn
        self.fence = fence
        self.lease_s = lease_s
        self.lookup_time = lookup_time
        self.released = False

    def _is_expired(self):
        if self.released:
            return True
        return time.monotonic() - self.lookup_time > self.lease_s

    def is_expired(self):
        with self.semaphore:
            return self._is_expired()

    def update_rvn(self, rvn, last_update, lease_s):
        self.rvn = rvn
        self.lookup_time = last_update
        self.lease_s = lease_s


class StoredLock:
    """What a consistent read of the lock item returned, and when."""

    def __init__(self, item, lookup_time):
        self.owner = item["ownerName"]["S"]
        self.rvn = item["recordVersionNumber"]["S"]
        self.lease_s = int(item["leaseDuration"]["N"]) / 1000.0
        self.fence = int(item.get("fence", {"N": "0"})["N"])
        self.lookup_time = lookup_time

    def is_expired(self, extra_s=0.0):
        return time.monotonic() - self.lookup_time > self.lease_s + extra_s


class LockClient:
    def __init__(self, ddb, table, owner, lease_s, heartbeat_s, refresh_s, buffer_s):
        if lease_s < 2 * heartbeat_s:
            raise ValueError("heartbeat period must be no more than half the lease duration")
        self.ddb = ddb
        self.table = table
        self.owner = owner
        self.lease_s = lease_s
        self.heartbeat_s = heartbeat_s
        self.refresh_s = refresh_s
        self.buffer_s = buffer_s
        self._locks = {}
        self._locks_mu = threading.Lock()
        self._closed = threading.Event()
        self._hb_thread = None

    # -- reads ---------------------------------------------------------------

    def get_lock_item(self, key):
        resp = self.ddb.get_item(
            TableName=self.table,
            Key={"key": {"S": key}},
            ConsistentRead=True,
        )
        # Whoever reads the lock errs on the side of not expiring it: the
        # lease clock for what we saw starts after the read returned.
        lookup_time = time.monotonic()
        item = resp.get("Item")
        if not item:
            return None
        return StoredLock(item, lookup_time)

    # -- acquisition ---------------------------------------------------------

    def _put_lock(self, key, fence, condition, names, values):
        rvn = new_record_version_number()
        item = {
            "key": {"S": key},
            "ownerName": {"S": self.owner},
            "recordVersionNumber": {"S": rvn},
            "leaseDuration": {"N": str(int(self.lease_s * 1000))},
            "fence": {"N": str(fence)},
        }
        last_updated = time.monotonic()
        kwargs = dict(TableName=self.table, Item=item, ConditionExpression=condition,
                      ExpressionAttributeNames=names)
        if values:
            kwargs["ExpressionAttributeValues"] = values
        try:
            self.ddb.put_item(**kwargs)
        except ClientError as err:
            err = _parse_dynamodb_error(err, "cannot store lock item: lock already acquired by other client")
            if isinstance(err, LockNotGranted):
                return None
            raise err
        lock = Lock(key, self.owner, rvn, fence, self.lease_s, last_updated)
        with self._locks_mu:
            self._locks[key] = lock
        return lock

    def _store_lock(self, key, state):
        existing = self.get_lock_item(key)
        if existing is None:
            return self._put_lock(key, 1, "attribute_not_exists(#k)", {"#k": "key"}, None)

        tracked = state.get("tracking")
        if tracked is None or tracked.rvn != existing.rvn:
            # Someone holds it, or renewed it since we last looked: start
            # (re)counting a full lease from this read.
            state["tracking"] = existing
            return None
        if tracked.is_expired(self.buffer_s):
            log.info("rvn %s unchanged for %.1fs, taking over from %s (fence %d)",
                     tracked.rvn, tracked.lease_s + self.buffer_s, tracked.owner, tracked.fence)
            state["tracking"] = None
            return self._put_lock(
                key,
                tracked.fence + 1,
                "attribute_exists(#k) AND #rvn = :rvn",
                {"#k": "key", "#rvn": "recordVersionNumber"},
                {":rvn": {"S": tracked.rvn}},
            )
        return None

    def acquire(self, key):
        """Block until this client holds the lock."""
        state = {}
        while not self._closed.is_set():
            try:
                lock = self._store_lock(key, state)
            except (ClientError,) + _DEADLINE_ERRORS as err:
                log.warning("lock lookup failed: %s", err)
                lock = None
            if lock is not None:
                return lock
            self._closed.wait(self.refresh_s)
        raise LockNotGranted("client closed")

    def holds(self, lock):
        with self._locks_mu:
            current = self._locks.get(lock.key)
        return current is lock and not lock.is_expired()

    # -- heartbeats ----------------------------------------------------------

    def send_heartbeat(self, lock, retries=0, retry_wait_s=0.0):
        """Renew the lease on ``lock``.

        ``retries`` re-reads the item after a failed attempt and retries the
        heartbeat against what is stored.
        """
        with lock.semaphore:
            current = lock.rvn
            if not current:
                raise LockNotGranted("cannot send heartbeats to a read-only lock")
            target = new_record_version_number()
            try:
                self._send_heartbeat(lock, current, target)
            except _DEADLINE_ERRORS as err:
                raise HeartbeatDeadlineExceeded(str(err)) from err
            except Exception as err:
                err = self._retry_heartbeat(err, lock, current, target, retries, retry_wait_s)
                if err is None:
                    return
                err = _parse_dynamodb_error(err, "already acquired lock, stopping heartbeats")
                if isinstance(err, LockNotGranted):
                    with self._locks_mu:
                        if self._locks.get(lock.key) is lock:
                            del self._locks[lock.key]
                raise err

    def _send_heartbeat(self, lock, current_rvn, target_rvn):
        if lock._is_expired() or lock.owner != self.owner or lock.released:
            raise LockNotGranted("cannot send heartbeat because lock is not granted")

        names = {"#k": "key", "#owner": "ownerName", "#rvn": "recordVersionNumber",
                 "#lease": "leaseDuration"}
        values = {":owner": {"S": lock.owner}, ":cur": {"S": current_rvn},
                  ":target": {"S": target_rvn}, ":lease": {"N": str(int(self.lease_s * 1000))}}

        last_update = time.monotonic()
        self.ddb.update_item(
            TableName=self.table,
            Key={"key": {"S": lock.key}},
            ConditionExpression="attribute_exists(#k) AND #rvn = :cur AND #owner = :owner",
            UpdateExpression="SET #lease = :lease, #rvn = :target",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        lock.update_rvn(target_rvn, last_update, self.lease_s)

    def _retry_heartbeat(self, err_heartbeat, lock, current_rvn, target_rvn, retries, wait_s):
        rvn = current_rvn
        for attempt in range(retries):
            log.info("retrying heartbeat... attempt %d", attempt)
            try:
                stored = self.get_lock_item(lock.key)
            except Exception as err:
                return Exception("cannot load lock for heartbeat retry: %s" % err)
            if stored is None or (stored.rvn != current_rvn and stored.rvn != target_rvn):
                return LockNotGranted("lock lost during heartbeat")
            if stored.rvn == target_rvn:
                # The "failed" write actually landed.
                rvn = target_rvn
            try:
                self._send_heartbeat(lock, rvn, target_rvn)
                return None
            except Exception as err:
                err_heartbeat = err
            log.info("heartbeat retry, attempt %d, waiting %.2fs before next attempt", attempt, wait_s)
            time.sleep(wait_s)
        return err_heartbeat

    def _heartbeat_loop(self):
        log.debug("heartbeats starting")
        next_tick = time.monotonic() + self.heartbeat_s
        while not self._closed.wait(max(0.0, next_tick - time.monotonic())):
            next_tick += self.heartbeat_s
            with self._locks_mu:
                locks = list(self._locks.values())
            for lock in locks:
                try:
                    self.send_heartbeat(lock)
                except Exception as err:
                    log.warning("error sending heartbeat to %s: %s", lock.key, err)
            now = time.monotonic()
            if next_tick < now:
                next_tick = now
        log.debug("heartbeats done")

    def start_heartbeats(self):
        self._hb_thread = threading.Thread(target=self._heartbeat_loop, name="heartbeat", daemon=True)
        self._hb_thread.start()

    # -- release ---------------------------------------------------------------

    def release(self, lock):
        """Give ``lock`` up so that a standby can take it without waiting.

        Same as the Go client's release when it is set to delete the lock: the
        item is deleted, conditioned on our owner name and the version tag we
        last wrote, so a lock that somebody else holds by now is left alone.
        """
        with lock.semaphore:
            lock.released = True
            with self._locks_mu:
                if self._locks.get(lock.key) is lock:
                    del self._locks[lock.key]
            try:
                self.ddb.delete_item(
                    TableName=self.table,
                    Key={"key": {"S": lock.key}},
                    ConditionExpression="attribute_exists(#k) AND #rvn = :rvn AND #owner = :owner",
                    ExpressionAttributeNames={"#k": "key", "#rvn": "recordVersionNumber",
                                              "#owner": "ownerName"},
                    ExpressionAttributeValues={":rvn": {"S": lock.rvn}, ":owner": {"S": lock.owner}},
                )
            except ClientError as err:
                err = _parse_dynamodb_error(err, "cannot release lock: held by another client")
                raise err

    def close(self):
        """Stop heartbeating and release every lock this client holds."""
        self._closed.set()
        with self._locks_mu:
            locks = list(self._locks.values())
        for lock in locks:
            try:
                self.release(lock)
                log.info("released %s (fence %d)", lock.key, lock.fence)
            except Exception as err:
                log.warning("cannot release %s: %s", lock.key, err)
