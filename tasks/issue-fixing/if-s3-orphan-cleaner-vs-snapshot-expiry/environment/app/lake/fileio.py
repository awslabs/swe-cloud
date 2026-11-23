"""S3 access for one table: every path below is relative to the table prefix."""

import json
import os
import time
from dataclasses import dataclass

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

READ_FILE_RETRY_NUM = 3
READ_FILE_RETRY_INTERVAL_S = 0.005

_NOT_FOUND = {"404", "NoSuchKey", "NotFound"}


@dataclass(frozen=True)
class FileStatus:
    path: str  # relative to the table prefix
    size: int
    modification_time_ms: int


def make_client(endpoint_url=None):
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url or os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566"),
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "test"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "test"),
        config=Config(
            s3={"addressing_style": "path"},
            retries={"max_attempts": 2, "mode": "standard"},
            connect_timeout=5,
            read_timeout=30,
            max_pool_connections=32,
        ),
    )


def _is_not_found(error):
    code = str(error.response.get("Error", {}).get("Code", ""))
    status = str(error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", ""))
    return code in _NOT_FOUND or status == "404"


class S3FileIO:
    def __init__(self, bucket, table_prefix, endpoint_url=None, client=None):
        self.bucket = bucket
        self.prefix = table_prefix if table_prefix.endswith("/") else table_prefix + "/"
        self.client = client or make_client(endpoint_url)

    def key(self, path):
        return self.prefix + path

    def read_bytes(self, path):
        """Raises FileNotFoundError when the object does not exist."""
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self.key(path))
        except ClientError as e:
            if _is_not_found(e):
                raise FileNotFoundError(path) from e
            raise IOError(f"failed to read {path}: {e}") from e
        return response["Body"].read()

    def read_json(self, path):
        return json.loads(self.read_bytes(path))

    def read_utf8(self, path):
        return self.read_bytes(path).decode("utf-8")

    def exists(self, path):
        try:
            self.client.head_object(Bucket=self.bucket, Key=self.key(path))
            return True
        except ClientError as e:
            if _is_not_found(e):
                return False
            raise IOError(f"failed to stat {path}: {e}") from e

    def list_status(self, directory):
        """All objects under `directory` (relative, ending in '/')."""
        result = []
        paginator = self.client.get_paginator("list_objects_v2")
        try:
            for page in paginator.paginate(Bucket=self.bucket, Prefix=self.key(directory)):
                for item in page.get("Contents", []):
                    result.append(
                        FileStatus(
                            path=item["Key"][len(self.prefix):],
                            size=int(item.get("Size", 0)),
                            modification_time_ms=int(item["LastModified"].timestamp() * 1000),
                        )
                    )
        except ClientError as e:
            raise IOError(f"failed to list {directory}: {e}") from e
        return result

    def delete(self, path):
        try:
            self.client.delete_object(Bucket=self.bucket, Key=self.key(path))
        except ClientError as e:
            raise IOError(f"failed to delete {path}: {e}") from e

    # writer side (ingestion jobs, lakectl); the maintenance role cannot write

    def write_bytes(self, path, data):
        self.client.put_object(Bucket=self.bucket, Key=self.key(path), Body=data)

    def write_json(self, path, obj):
        self.write_bytes(path, json.dumps(obj, sort_keys=True).encode("utf-8"))

    def write_utf8(self, path, text):
        self.write_bytes(path, text.encode("utf-8"))


def retry_io(reader, retries=READ_FILE_RETRY_NUM, interval_s=READ_FILE_RETRY_INTERVAL_S):
    """Retry transient IO errors; FileNotFoundError is not transient and propagates."""
    caught = None
    for _ in range(retries):
        try:
            return reader()
        except FileNotFoundError:
            raise
        except IOError as e:
            caught = e
        time.sleep(interval_s)
    raise caught
