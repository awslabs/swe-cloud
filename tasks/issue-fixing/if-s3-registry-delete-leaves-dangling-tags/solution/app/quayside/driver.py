"""S3 storage driver.

Paths are object keys. A "directory" is a key prefix; it exists while any object lives under it.
"""

import logging
import threading

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from .errors import PathNotFoundError

log = logging.getLogger("quayside.driver")

_NOT_FOUND = {"NoSuchKey", "404", "NotFound"}
_PRECONDITION = {"PreconditionFailed", "412"}


class PreconditionFailed(Exception):
    def __init__(self, path):
        super().__init__(f"precondition failed: {path}")
        self.path = path


def _code(err):
    return err.response.get("Error", {}).get("Code", "")


def _status(err):
    return err.response.get("ResponseMetadata", {}).get("HTTPStatusCode")


class S3Driver:
    def __init__(self, endpoint, bucket, region="us-east-1"):
        self.bucket = bucket
        self._endpoint = endpoint
        self._region = region
        self._lock = threading.Lock()
        self.client = self._new_client()

    def _new_client(self):
        return boto3.client(
            "s3",
            endpoint_url=self._endpoint,
            region_name=self._region,
            config=Config(
                s3={"addressing_style": "path"},
                retries={"max_attempts": 3, "mode": "standard"},
                max_pool_connections=32,
                connect_timeout=5,
                read_timeout=60,
            ),
        )

    def _conditional_failed(self, err, path):
        if _code(err) in _PRECONDITION or _status(err) == 412:
            # Some S3 implementations break the connection that carried a 412; start over
            # with a fresh connection pool.
            with self._lock:
                self.client = self._new_client()
            raise PreconditionFailed(path) from None

    def get_content(self, path):
        return self.get_with_etag(path)[0]

    def get_with_etag(self, path):
        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=path)
        except ClientError as err:
            if _code(err) in _NOT_FOUND:
                raise PathNotFoundError(path) from None
            raise
        return resp["Body"].read(), resp["ETag"]

    def put_content(self, path, content, if_match=None):
        """Write path. With if_match, only if the object's current ETag is if_match."""
        kwargs = {"Bucket": self.bucket, "Key": path, "Body": content}
        if if_match is not None:
            kwargs["IfMatch"] = if_match
        try:
            resp = self.client.put_object(**kwargs)
        except ClientError as err:
            if if_match is not None:
                if _code(err) in _NOT_FOUND:
                    raise PathNotFoundError(path) from None
                self._conditional_failed(err, path)
            raise
        return resp["ETag"]

    def delete_object(self, path, if_match=None):
        """Delete the single object at path. With if_match, only if its ETag is if_match."""
        kwargs = {"Bucket": self.bucket, "Key": path}
        if if_match is not None:
            kwargs["IfMatch"] = if_match
        try:
            self.client.delete_object(**kwargs)
        except ClientError as err:
            if _code(err) in _NOT_FOUND:
                raise PathNotFoundError(path) from None
            if if_match is not None:
                self._conditional_failed(err, path)
            raise

    def list(self, path):
        """Return the immediate children of path (full paths, no trailing slash)."""
        prefix = path.rstrip("/") + "/"
        children = []
        token = None
        while True:
            kwargs = {"Bucket": self.bucket, "Prefix": prefix, "Delimiter": "/"}
            if token:
                kwargs["ContinuationToken"] = token
            resp = self.client.list_objects_v2(**kwargs)
            children.extend(p["Prefix"].rstrip("/") for p in resp.get("CommonPrefixes", []))
            children.extend(o["Key"] for o in resp.get("Contents", []))
            if not resp.get("IsTruncated"):
                break
            token = resp["NextContinuationToken"]
        if not children:
            raise PathNotFoundError(path)
        return children

    def delete(self, path):
        """Recursively delete the object at path and everything under path/."""
        keys = []
        token = None
        while True:
            kwargs = {"Bucket": self.bucket, "Prefix": path}
            if token:
                kwargs["ContinuationToken"] = token
            resp = self.client.list_objects_v2(**kwargs)
            for obj in resp.get("Contents", []):
                key = obj["Key"]
                if key == path or key.startswith(path.rstrip("/") + "/"):
                    keys.append(key)
            if not resp.get("IsTruncated"):
                break
            token = resp["NextContinuationToken"]
        if not keys:
            raise PathNotFoundError(path)
        for i in range(0, len(keys), 1000):
            batch = keys[i:i + 1000]
            resp = self.client.delete_objects(
                Bucket=self.bucket,
                Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
            )
            errors = resp.get("Errors", [])
            if errors:
                raise RuntimeError(f"delete {path}: {len(errors)} keys failed: {errors[0]}")
