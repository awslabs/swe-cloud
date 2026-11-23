"""S3 repository I/O for the full-backup service."""
import json
import os
import re

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

LABEL = re.compile(r"^[0-9]{8}-[0-9]{6}F$")
CATALOG = "backup/backup.info"


def client():
    return boto3.client(
        "s3", endpoint_url=os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566"),
        region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test",
        config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 2},
                      connect_timeout=5, read_timeout=20))


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class Repository:
    def __init__(self, bucket):
        self.bucket = bucket
        self.s3 = client()

    def reconnect(self):
        self.s3.close()
        self.s3 = client()

    def get(self, key):
        result = self.s3.get_object(Bucket=self.bucket, Key=key)
        body = result["Body"].read()
        result["Body"].close()
        return body, result["ETag"]

    def put(self, key, body, etag=None):
        args = {"Bucket": self.bucket, "Key": key, "Body": body}
        if etag is not None:
            args["IfMatch"] = etag
        return self.s3.put_object(**args)["ETag"]

    def exists(self, key):
        try:
            self.s3.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as exc:
            if exc.response["ResponseMetadata"]["HTTPStatusCode"] == 404:
                return False
            raise

    def objects(self, prefix="backup/"):
        result = {}
        for page in self.s3.get_paginator("list_objects_v2").paginate(
                Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                result[obj["Key"]] = obj["ETag"]
        return result

    def labels(self):
        return sorted({key.split("/")[1] for key in self.objects()
                       if len(key.split("/")) > 2 and LABEL.fullmatch(key.split("/")[1])})

    def delete(self, key):
        self.s3.delete_object(Bucket=self.bucket, Key=key)

    def remove(self, label):
        for key in self.objects(f"backup/{label}/"):
            self.delete(key)

    def load(self):
        body, etag = self.get(CATALOG)
        info = json.loads(body)
        if info["format"] != 1:
            raise ValueError("unsupported repository format")
        return info, etag

    def save(self, info, etag=None):
        return self.put(CATALOG, encode(info), etag)

    def manifest(self, label):
        body, _ = self.get(f"backup/{label}/backup.manifest")
        return json.loads(body)


def entry(manifest):
    return {"type": "full", "prior": None, "files": manifest["files"]}
