"""S3 backend for the disk cache (port of bazel-remote's cache/s3proxy).

Objects live in one bucket:

    ac/<hash[:2]>/<hash>       action-cache entries (JSON)
    cas.v2/<hash[:2]>/<hash>   content-addressed blobs (raw bytes here; zstd upstream)

The bucket is bounded by lifecycle Expiration rules on each prefix, which count from an
object's LastModified. A read never moves LastModified; a PutObject or a CopyObject of the key
onto itself with MetadataDirective=REPLACE does.
"""

import logging
import posixpath
import queue
import threading
import time

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

AC = "ac"
CAS = "cas"

log = logging.getLogger("cachesvc.s3")
access_log = logging.getLogger("cachesvc.access")


def object_key(prefix, hash_, kind):
    if kind == CAS:
        # "cas.v2" distinguishes the current blob format from the old one.
        base = posixpath.join("cas.v2", hash_[:2], hash_)
    else:
        base = posixpath.join(kind, hash_[:2], hash_)
    return posixpath.join(prefix, base) if prefix else base


def _not_found(err):
    code = err.response.get("Error", {}).get("Code", "")
    status = err.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in ("NoSuchKey", "404", "NotFound") or status == 404


def make_client(endpoint, max_pool):
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        config=BotoConfig(
            s3={"addressing_style": "path"},
            # Fail fast: the request path has the disk tier and Bazel's own retries behind it,
            # and a botocore retry loop on a FindMissing stalls the build.
            retries={"total_max_attempts": 1, "mode": "standard"},
            connect_timeout=5,
            read_timeout=30,
            max_pool_connections=max_pool,
        ),
    )


class S3Cache:
    def __init__(self, endpoint, bucket, prefix="", update_timestamps=False, cas_refresh_age=0.0,
                 num_uploaders=4, max_queued_uploads=1000, client=None):
        self.bucket = bucket
        self.prefix = prefix
        self.update_timestamps = update_timestamps
        self.cas_refresh_age = cas_refresh_age
        self.client = client or make_client(endpoint, max_pool=64)

        self.refresh_queue = None
        self._refreshing = set()  # keys queued, being copied, or being looked up for their age
        self._refreshing_lock = threading.Lock()
        # CAS key -> a lower bound on its LastModified in the bucket. Disk hits never reach S3,
        # so this is the only way the disk tier can tell how close a blob is to expiring. Only
        # writes and lookups that succeeded move it; a failed copy leaves it where it was.
        self._s3_modified = {}

        if self.cas_refresh_age > 0 and not self.update_timestamps:
            self.refresh_queue = queue.Queue(maxsize=max_queued_uploads)
            for i in range(num_uploaders):
                threading.Thread(target=self._refresh_worker, name=f"refresh-{i}",
                                 daemon=True).start()

    def object_key(self, hash_, kind):
        return object_key(self.prefix, hash_, kind)

    # -- writes -------------------------------------------------------------------------------

    def put(self, kind, hash_, data):
        """Upload one object. Raises on failure.

        Upstream queues uploads to a pool of uploaders. Here the caller waits, so that a PUT is
        only acknowledged once the object is in the bucket and Bazel never writes an AC entry
        ahead of the blobs it lists.
        """
        key = self.object_key(hash_, kind)
        err = None
        sent = time.time()
        try:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=data,
                                   ContentType="application/octet-stream")
        except Exception as e:  # noqa: BLE001
            err = e
            raise
        finally:
            _log_response("UPLOAD", self.bucket, key, err)
        if kind == CAS:
            self._note_modified(key, sent)

    def update_modification_timestamp(self, key):
        err = None
        try:
            self.client.copy_object(Bucket=self.bucket, Key=key,
                                    CopySource={"Bucket": self.bucket, "Key": key},
                                    MetadataDirective="REPLACE")
        except Exception as e:  # noqa: BLE001
            err = e
        _log_response("COMPOSE", self.bucket, key, err)

    # -- cas_refresh_age ----------------------------------------------------------------------

    def _note_modified(self, key, last_modified):
        with self._refreshing_lock:
            if last_modified > self._s3_modified.get(key, 0.0):
                self._s3_modified[key] = last_modified

    def note_local_hit(self, hash_):
        """The disk tier answered a CAS lookup itself: treat it as a use of the S3 copy.

        Never blocks and never makes an S3 request on the caller's thread. If the blob's age in
        S3 is unknown (it was on disk before this process started), one background HEAD finds
        it out and feeds the same age check.
        """
        if self.refresh_queue is None:
            return
        key = self.object_key(hash_, CAS)
        with self._refreshing_lock:
            last_modified = self._s3_modified.get(key)
        if last_modified is not None:
            self.queue_cas_refresh(CAS, key, last_modified)
            return
        with self._refreshing_lock:
            if key in self._refreshing:
                return
            self._refreshing.add(key)
        try:
            self.refresh_queue.put_nowait(("probe", key))
        except queue.Full:
            with self._refreshing_lock:
                self._refreshing.discard(key)
            log.error("too many refreshes queued")

    def queue_cas_refresh(self, kind, key, last_modified):
        """Copy a CAS blob onto itself in the background once it is older than cas_refresh_age.

        A lifecycle rule that expires objects by last write then never deletes a blob that is
        still being found or fetched, so AC entries written in the meantime keep their outputs.
        """
        if self.refresh_queue is None or kind != CAS:
            return
        if time.time() - last_modified < self.cas_refresh_age:
            return

        # A blob that many lookups find at once is copied once; repeated copies of one key
        # would also be throttled by S3.
        with self._refreshing_lock:
            if key in self._refreshing:
                return
            self._refreshing.add(key)

        try:
            self.refresh_queue.put_nowait(("copy", key))
        except queue.Full:
            with self._refreshing_lock:
                self._refreshing.discard(key)
            log.error("too many refreshes queued")

    def _refresh_worker(self):
        while True:
            op, key = self.refresh_queue.get()
            if op == "probe":
                self._probe(key)
                continue
            err = None
            sent = time.time()
            try:
                resp = self.client.copy_object(
                    Bucket=self.bucket, Key=key,
                    CopySource={"Bucket": self.bucket, "Key": key},
                    MetadataDirective="REPLACE",
                    ContentType="application/octet-stream",
                )
                result = resp.get("CopyObjectResult", {}).get("LastModified")
                self._note_modified(key, max(sent, result.timestamp()) if result else sent)
            except Exception as e:  # noqa: BLE001
                # Throttled or failed: the age is unchanged, so the next use queues it again.
                err = e
            with self._refreshing_lock:
                self._refreshing.discard(key)
            _log_response("REFRESH", self.bucket, key, err)

    def _probe(self, key):
        last_modified = None
        err = None
        try:
            last_modified = self.client.head_object(Bucket=self.bucket, Key=key)[
                "LastModified"].timestamp()
            self._note_modified(key, last_modified)
        except Exception as e:  # noqa: BLE001
            err = e
        with self._refreshing_lock:
            self._refreshing.discard(key)
        _log_response("AGE", self.bucket, key, err)
        if last_modified is not None:
            self.queue_cas_refresh(CAS, key, last_modified)

    # -- reads --------------------------------------------------------------------------------

    def get(self, kind, hash_):
        """Return the object's bytes, or None if the bucket does not have it."""
        key = self.object_key(hash_, kind)
        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if _not_found(e):
                _log_response("DOWNLOAD", self.bucket, key, "NOT FOUND")
                return None
            _log_response("DOWNLOAD", self.bucket, key, e)
            raise
        data = resp["Body"].read()

        if self.update_timestamps:
            self.update_modification_timestamp(key)
        if kind == CAS:
            self._note_modified(key, resp["LastModified"].timestamp())
        self.queue_cas_refresh(kind, key, resp["LastModified"].timestamp())

        _log_response("DOWNLOAD", self.bucket, key, None)
        return data

    def contains(self, kind, hash_):
        """HEAD the object. Returns (exists, size)."""
        key = self.object_key(hash_, kind)
        try:
            resp = self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            _log_response("CONTAINS", self.bucket, key, "NOT FOUND" if _not_found(e) else e)
            return False, -1
        except Exception as e:  # noqa: BLE001
            _log_response("CONTAINS", self.bucket, key, e)
            return False, -1

        if kind == CAS:
            self._note_modified(key, resp["LastModified"].timestamp())
        self.queue_cas_refresh(kind, key, resp["LastModified"].timestamp())
        _log_response("CONTAINS", self.bucket, key, None)
        return True, resp["ContentLength"]


def _log_response(method, bucket, key, err):
    status = "OK" if err is None else str(err)
    access_log.info("S3 %s %s %s %s", method, bucket, key, status)
