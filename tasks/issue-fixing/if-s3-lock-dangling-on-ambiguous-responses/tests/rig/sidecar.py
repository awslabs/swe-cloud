"""The compaction sidecar H: a real contender on the same lock object, driven by the test.

H talks to the emulator directly (not through a gateway). It locks under the faulted worker's
configured owner name, exactly as the real compaction sidecar does (same code, same --owner). It
acquires in two ways:

* ``acquire_fresh`` — a normal acquisition with its own lease end, used while the worker's own
  earlier write is unresolved;
* ``acquire_colliding`` — an acquisition that reuses the lease end the worker's intercepted write
  carried, i.e. the sidecar happening to acquire inside the same whole second as the worker.

In both cases H writes the three required fields plus its own random marker, holds the lock for a
fixed window (running one ledger commit as worker ``compactor``), then releases it. If H's own
release is rejected, another writer expired or took its live lease — that is the mutual-exclusion
violation, and H records it.
"""
import json
import threading
import time
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from .gateway import norm_etag


class Sidecar:
    def __init__(self, rig, owner, hold_ms, lease_ms):
        self.rig = rig
        self.owner = owner
        self.hold_ms = hold_ms
        self.lease_ms = lease_ms
        self._s3 = boto3.client("s3", config=Config(retries={"max_attempts": 3, "mode": "standard"}))
        self.landed = []            # etags H successfully wrote
        self.lease_stolen = False   # set if H's own release was rejected
        self.acquisitions = []      # (etag, body) for each acquire H landed

    def _get(self):
        resp = self._s3.get_object(Bucket=self.rig.bucket, Key=self.rig.lock_key)
        body = json.loads(resp["Body"].read())
        return norm_etag(resp["ETag"]), body

    def _held(self, body):
        now = int(time.time() * 1000)
        return (not bool(body.get("expired"))
                and now <= int(body.get("validUntil", 0)) + self.rig.drift_buffer_ms)

    def _wait_free(self, deadline_s):
        end = time.monotonic() + deadline_s
        while time.monotonic() < end:
            etag, body = self._get()
            if not self._held(body):
                return etag, body
            time.sleep(0.05)
        return self._get()

    def _acquire_with(self, valid_until):
        etag, _ = self._wait_free(6.0)
        body = {"owner": self.owner, "expired": False, "validUntil": int(valid_until),
                "sidecar_acq": uuid.uuid4().hex}
        resp = self._s3.put_object(Bucket=self.rig.bucket, Key=self.rig.lock_key,
                                   Body=json.dumps(body).encode(), IfMatch=etag,
                                   ContentType="application/json")
        new_etag = norm_etag(resp["ETag"])
        w = self.rig.record_lock_write("H", etag, body, new_etag)
        self.landed.append(new_etag)
        self.acquisitions.append((new_etag, body))
        return new_etag, body

    def _run_section_and_release(self, etag, body):
        try:
            self._commit_ledger(etag)
            time.sleep(self.hold_ms / 1000.0)
            expired = dict(body)
            expired["expired"] = True
            resp = self._s3.put_object(Bucket=self.rig.bucket, Key=self.rig.lock_key,
                                       Body=json.dumps(expired).encode(), IfMatch=etag,
                                       ContentType="application/json")
            new_etag = norm_etag(resp["ETag"])
            self.rig.record_lock_write("H", etag, expired, new_etag)
            self.landed.append(new_etag)
        except ClientError as e:
            status = int(e.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
            if status == 412:
                # Another writer expired or took H's live lease: the mutual-exclusion violation.
                self.lease_stolen = True
            else:
                raise

    def _commit_ledger(self, lock_etag):
        counter_key = f"{self.rig.ledger_prefix}counter.json"
        cur = json.loads(self._s3.get_object(Bucket=self.rig.bucket, Key=counter_key)["Body"].read())
        seq = int(cur["seq"]) + 1
        entry = {"seq": seq, "worker": "compactor", "section": 0, "lock_etag": lock_etag}
        self._s3.put_object(Bucket=self.rig.bucket,
                            Key=f"{self.rig.ledger_prefix}entries/{seq:06d}-compactor-0.json",
                            Body=json.dumps(entry).encode(), ContentType="application/json")
        body = json.dumps({"seq": seq}).encode()
        self._s3.put_object(Bucket=self.rig.bucket, Key=counter_key, Body=body,
                            ContentType="application/json")
        self.rig.record_counter_write("H", body)

    def acquire_fresh(self, done_evt):
        """Acquire with H's own lease end, hold and release in the background. Returns the etag."""
        valid_until = (int(time.time() * 1000) // 1000) * 1000 + self.lease_ms
        etag, body = self._acquire_with(valid_until)
        threading.Thread(target=self._hold_then_signal, args=(etag, body, done_evt),
                         daemon=True).start()
        return etag

    def acquire_colliding(self, worker_valid_until, done_evt):
        """Acquire reusing the worker's lease end (same-second collision)."""
        etag, body = self._acquire_with(worker_valid_until)
        threading.Thread(target=self._hold_then_signal, args=(etag, body, done_evt),
                         daemon=True).start()
        return etag

    def _hold_then_signal(self, etag, body, done_evt):
        try:
            self._run_section_and_release(etag, body)
        finally:
            done_evt.set()
