"""S3 access to the lock object, using ETag preconditions.

Every write to the lock object is a PutObject conditional on the ETag of the version the caller
last read or wrote. The object is created once by ``bootstrap.py`` and is never deleted, so there
is no create path here.
"""
import enum
import logging

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from .lockfile import LockData, LockFile

log = logging.getLogger("tablelock.storage")

PRECONDITION_FAILED = 412
NOT_FOUND = 404
CONDITIONAL_REQUEST_CONFLICT = 409
RATE_LIMITED = 429
SERVER_ERROR_MIN = 500


class GetResult(enum.Enum):
    SUCCESS = "SUCCESS"
    NOT_EXISTS = "NOT_EXISTS"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


class UpsertResult(enum.Enum):
    SUCCESS = "SUCCESS"
    ACQUIRED_BY_OTHERS = "ACQUIRED_BY_OTHERS"
    THROTTLED = "THROTTLED"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


def _status(err: ClientError) -> int:
    return int(err.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))


class S3LockClient:
    def __init__(self, owner: str, bucket: str, key: str, lease_seconds: int):
        self.owner = owner
        self.bucket = bucket
        self.key = key
        # Every call gets a fifth of the lease, and no SDK retries: the provider sees every
        # answer storage gives and decides itself what to do with it.
        call_timeout = max(1, lease_seconds // 5)
        self._config = Config(
            retries={"total_max_attempts": 1},
            connect_timeout=call_timeout,
            read_timeout=call_timeout,
        )
        self._s3 = self._new_client()

    def _new_client(self):
        return boto3.client("s3", config=self._config)

    def _reconnect(self):
        # After a rejected conditional put this endpoint leaves the pooled keep-alive
        # connection unusable for the next request; start from a fresh pool.
        self._s3 = self._new_client()

    def read_current(self):
        """GET the lock object. Returns (GetResult, LockFile | None)."""
        try:
            resp = self._s3.get_object(Bucket=self.bucket, Key=self.key)
            body = resp["Body"].read()
            return GetResult.SUCCESS, LockFile(LockData.from_bytes(body), resp["ETag"])
        except ClientError as e:
            status = _status(e)
            if status == NOT_FOUND:
                log.info("Owner %s: lock object not found: %s", self.owner, self.key)
                return GetResult.NOT_EXISTS, None
            if status == CONDITIONAL_REQUEST_CONFLICT:
                log.info("Owner %s: conflicting operation on %s", self.owner, self.key)
            elif status == RATE_LIMITED:
                log.warning("Owner %s: rate limit exceeded reading %s", self.owner, self.key)
            elif status >= SERVER_ERROR_MIN:
                log.warning("Owner %s: storage server error reading %s: %s", self.owner, self.key, e)
            else:
                raise
            return GetResult.UNKNOWN_ERROR, None

    def try_upsert(self, new_data: LockData, previous: LockFile):
        """Write ``new_data`` over ``previous`` (If-Match its ETag).

        Returns (UpsertResult, LockFile | None); the LockFile carries the new ETag on success.
        """
        try:
            resp = self._s3.put_object(
                Bucket=self.bucket,
                Key=self.key,
                Body=new_data.to_bytes(),
                IfMatch=previous.etag,
                ContentType="application/json",
            )
            return UpsertResult.SUCCESS, LockFile(new_data, resp["ETag"])
        except ClientError as e:
            return self._classify_put_error(e), None
        except BotoCoreError as e:
            log.error("Owner %s: unexpected SDK error while writing lock object %s: %r",
                      self.owner, self.key, e)
            return UpsertResult.UNKNOWN_ERROR, None

    def _classify_put_error(self, e: ClientError) -> UpsertResult:
        status = _status(e)
        if status == PRECONDITION_FAILED:
            self._reconnect()
            log.info("Owner %s: lock object modified by another process: %s", self.owner, self.key)
            return UpsertResult.ACQUIRED_BY_OTHERS
        if status == CONDITIONAL_REQUEST_CONFLICT:
            log.info("Owner %s: retriable conditional request conflict: %s", self.owner, self.key)
        elif status == RATE_LIMITED:
            log.warning("Owner %s: rate limit exceeded for %s", self.owner, self.key)
            return UpsertResult.THROTTLED
        elif status >= SERVER_ERROR_MIN:
            log.warning("Owner %s: storage server error for %s: %s", self.owner, self.key, e)
        else:
            log.warning("Owner %s: error writing lock object %s: %s", self.owner, self.key, e)
        return UpsertResult.UNKNOWN_ERROR
