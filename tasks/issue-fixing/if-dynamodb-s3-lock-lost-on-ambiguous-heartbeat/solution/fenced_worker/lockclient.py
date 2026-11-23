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
    BotoCoreError,
    ClientError,
)

log = logging.getLogger("fenced_worker.lock")


class LockNotGranted(Exception):
    """The lock is not (or no longer) held by this client."""



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
        # Targets of heartbeats sent since the last confirmed one, with their
        # send times: any of them may have been applied.
        self.attempts = {}
        self.unresolved = False

    def lease_valid(self):
        return not self.released and time.monotonic() - self.lookup_time <= self.lease_s

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
        self._acquiring = {}
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
        # Like a heartbeat, an acquisition can be applied although it is
        # reported failed; the next read settles it by our version tag.
        self._acquiring[rvn] = (fence, last_updated)
        try:
            self.ddb.put_item(**kwargs)
        except ClientError as err:
            err = _parse_dynamodb_error(err, "cannot store lock item: lock already acquired by other client")
            if isinstance(err, LockNotGranted):
                return None
            raise err
        return self._adopt(key, rvn, fence, last_updated)

    def _adopt(self, key, rvn, fence, sent):
        self._acquiring.clear()
        lock = Lock(key, self.owner, rvn, fence, self.lease_s, sent)
        with self._locks_mu:
            self._locks[key] = lock
        return lock

    def _store_lock(self, key, state):
        existing = self.get_lock_item(key)
        if existing is not None and existing.owner == self.owner and existing.rvn in self._acquiring:
            fence, sent = self._acquiring[existing.rvn]
            log.info("acquisition with fence %d was applied after all", fence)
            return self._adopt(key, existing.rvn, fence, sent)
        if existing is None:
            # Releases keep the item, so a missing item means the lock was
            # never taken: fence 1 is the first epoch.
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
            except (ClientError, BotoCoreError) as err:
                log.warning("lock lookup failed: %s", err)
                lock = None
            if lock is not None:
                return lock
            self._closed.wait(self.refresh_s)
        raise LockNotGranted("client closed")

    def holds(self, lock):
        with self._locks_mu:
            current = self._locks.get(lock.key)
        return current is lock and lock.lease_valid()

    # -- heartbeats ----------------------------------------------------------

    def _drop(self, lock):
        lock.released = True
        with self._locks_mu:
            if self._locks.get(lock.key) is lock:
                del self._locks[lock.key]

    def release_local(self, lock):
        """Stop treating ``lock`` as held (a newer epoch exists)."""
        with lock.semaphore:
            self._drop(lock)

    def send_heartbeat(self, lock):
        """Renew the lease on ``lock``.

        A heartbeat whose outcome is unknown (timeout, reset, 5xx, or a
        conditional failure that an earlier applied attempt would explain) is
        resolved against the stored item before the next one is sent.
        """
        with lock.semaphore:
            if lock.released or lock.owner != self.owner:
                raise LockNotGranted("cannot send heartbeat because lock is not granted")
            if lock.unresolved and not self._resolve(lock):
                self._give_up_if_expired(lock)
                return
            if lock._is_expired():
                self._drop(lock)
                raise LockNotGranted("lease ran out")
            current = lock.rvn
            target = new_record_version_number()
            sent = time.monotonic()
            lock.attempts[target] = sent
            try:
                self._update(lock, current, target)
            except (ClientError, BotoCoreError) as err:
                log.warning("heartbeat outcome unknown (%s), checking the lock item", err)
                lock.unresolved = True
            else:
                lock.update_rvn(target, sent, self.lease_s)
                lock.attempts.clear()
                return
            if not self._resolve(lock):
                self._give_up_if_expired(lock)

    def _give_up_if_expired(self, lock):
        if lock._is_expired():
            self._drop(lock)
            raise LockNotGranted("lease ran out before the heartbeat outcome was known")

    def _resolve(self, lock):
        """Match the stored item against our writes. False if it could not be read."""
        try:
            stored = self.get_lock_item(lock.key)
        except (ClientError, BotoCoreError) as err:
            log.warning("cannot read lock item: %s", err)
            return False
        if stored is None or stored.owner != self.owner or stored.fence != lock.fence:
            self._drop(lock)
            raise LockNotGranted("lock taken over by %s" % (stored.owner if stored else None))
        if stored.rvn in lock.attempts:
            # Applied although reported failed. The lease it renewed started
            # when that request was sent, not now.
            lock.update_rvn(stored.rvn, lock.attempts[stored.rvn], self.lease_s)
        elif stored.rvn != lock.rvn:
            self._drop(lock)
            raise LockNotGranted("lock item carries a version tag we never wrote")
        lock.unresolved = False
        return True

    def _update(self, lock, current_rvn, target_rvn):
        self.ddb.update_item(
            TableName=self.table,
            Key={"key": {"S": lock.key}},
            ConditionExpression="attribute_exists(#k) AND #rvn = :cur AND #owner = :owner",
            UpdateExpression="SET #lease = :lease, #rvn = :target",
            ExpressionAttributeNames={"#k": "key", "#owner": "ownerName",
                                      "#rvn": "recordVersionNumber", "#lease": "leaseDuration"},
            ExpressionAttributeValues={":owner": {"S": lock.owner}, ":cur": {"S": current_rvn},
                                       ":target": {"S": target_rvn},
                                       ":lease": {"N": str(int(self.lease_s * 1000))}},
        )

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

        The item is kept, with its fence: deleting it would let the next
        acquirer start over at fence 1. The release writes a fresh version tag
        and a zero lease, so a standby's ordinary takeover rule applies after
        its buffer. It is conditioned on our owner name only, which is unique
        to this process, because a heartbeat may be in doubt.
        """
        with lock.semaphore:
            self._drop(lock)
            self.ddb.update_item(
                TableName=self.table,
                Key={"key": {"S": lock.key}},
                ConditionExpression="attribute_exists(#k) AND #owner = :owner AND #fence = :fence",
                UpdateExpression="SET #lease = :zero, #rvn = :rvn",
                ExpressionAttributeNames={"#k": "key", "#owner": "ownerName", "#fence": "fence",
                                          "#lease": "leaseDuration", "#rvn": "recordVersionNumber"},
                ExpressionAttributeValues={":owner": {"S": lock.owner},
                                           ":fence": {"N": str(lock.fence)},
                                           ":zero": {"N": "0"},
                                           ":rvn": {"S": new_record_version_number()}},
            )

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
