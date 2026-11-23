"""S3 storage driver.

Paths are object keys. A "directory" is a key prefix; it exists while any object lives under it.
"""

import logging

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from .errors import PathNotFoundError

log = logging.getLogger("quayside.driver")

_NOT_FOUND = {"NoSuchKey", "404", "NotFound"}


def _code(err):
    return err.response.get("Error", {}).get("Code", "")


class S3Driver:
    def __init__(self, endpoint, bucket, region="us-east-1"):
        self.bucket = bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name=region,
            config=Config(
                s3={"addressing_style": "path"},
                retries={"max_attempts": 3, "mode": "standard"},
                max_pool_connections=32,
                connect_timeout=5,
                read_timeout=60,
            ),
        )

    def get_content(self, path):
        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=path)
        except ClientError as err:
            if _code(err) in _NOT_FOUND:
                raise PathNotFoundError(path) from None
            raise
        return resp["Body"].read()

    def put_content(self, path, content):
        self.client.put_object(Bucket=self.bucket, Key=path, Body=content)

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
