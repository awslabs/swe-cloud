"""S3 access to the lock object. 5xx and connection errors are reported as AMBIGUOUS: the write
may or may not have landed, so the caller must reconcile rather than treat it as terminal.
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
    AMBIGUOUS = "AMBIGUOUS"       # 5xx / timeout / dropped connection; landing is unknown
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


def _status(err: ClientError) -> int:
    return int(err.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))


class S3LockClient:
    def __init__(self, owner, bucket, key, lease_seconds):
        self.owner = owner
        self.bucket = bucket
        self.key = key
        call_timeout = max(1, lease_seconds // 5)
        self._config = Config(retries={"total_max_attempts": 1},
                              connect_timeout=call_timeout, read_timeout=call_timeout)
        self._s3 = self._new_client()

    def _new_client(self):
        return boto3.client("s3", config=self._config)

    def _reconnect(self):
        self._s3 = self._new_client()

    def read_current(self):
        try:
            resp = self._s3.get_object(Bucket=self.bucket, Key=self.key)
            body = resp["Body"].read()
            return GetResult.SUCCESS, LockFile(LockData.from_bytes(body), resp["ETag"])
        except ClientError as e:
            status = _status(e)
            if status == NOT_FOUND:
                return GetResult.NOT_EXISTS, None
            log.warning("Owner %s: error reading lock object %s: %s", self.owner, self.key, e)
            return GetResult.UNKNOWN_ERROR, None

    def try_upsert(self, new_data: LockData, previous: LockFile):
        try:
            resp = self._s3.put_object(Bucket=self.bucket, Key=self.key,
                                       Body=new_data.to_bytes(), IfMatch=previous.etag,
                                       ContentType="application/json")
            return UpsertResult.SUCCESS, LockFile(new_data, resp["ETag"])
        except ClientError as e:
            return self._classify(e), None
        except BotoCoreError as e:
            # A dropped connection or a timeout: we do not know whether the write landed.
            log.warning("Owner %s: connection-level error writing %s: %r", self.owner, self.key, e)
            self._reconnect()
            return UpsertResult.AMBIGUOUS, None

    def _classify(self, e: ClientError) -> UpsertResult:
        status = _status(e)
        if status == PRECONDITION_FAILED:
            self._reconnect()
            return UpsertResult.ACQUIRED_BY_OTHERS
        if status == RATE_LIMITED:
            return UpsertResult.THROTTLED
        if status >= SERVER_ERROR_MIN:
            log.warning("Owner %s: storage server error for %s: %s", self.owner, self.key, e)
            self._reconnect()
            return UpsertResult.AMBIGUOUS
        if status == CONDITIONAL_REQUEST_CONFLICT:
            return UpsertResult.THROTTLED
        log.warning("Owner %s: error writing lock object %s: %s", self.owner, self.key, e)
        return UpsertResult.UNKNOWN_ERROR

