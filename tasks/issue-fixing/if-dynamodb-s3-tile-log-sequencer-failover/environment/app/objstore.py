"""Object storage client (S3).

Plain puts and gets under the configured prefix.  A put is durable
once it returns.  There is no versioning, no conditional request and
no object lock in this bucket: an object at a public coordinate is
whatever the last successful put left there.
"""

import boto3
from botocore.config import Config


class NotFoundError(Exception):
    pass


class ObjectStore:
    def __init__(self, config):
        self.bucket = config["bucket"]
        self.prefix = config.get("prefix", "").rstrip("/")
        self.client = boto3.client(
            "s3",
            endpoint_url=config["s3_endpoint"],
            config=Config(retries={"max_attempts": 2}, read_timeout=30,
                          connect_timeout=10),
        )

    def key(self, name: str) -> str:
        return (self.prefix + "/" + name) if self.prefix else name

    def put(self, name: str, blob: bytes):
        self.client.put_object(
            Bucket=self.bucket,
            Key=self.key(name),
            Body=blob,
        )

    def get(self, name: str) -> bytes:
        try:
            resp = self.client.get_object(
                Bucket=self.bucket, Key=self.key(name)
            )
        except self.client.exceptions.NoSuchKey:
            raise NotFoundError(name)
        return resp["Body"].read()

    def delete(self, name: str):
        self.client.delete_object(Bucket=self.bucket, Key=self.key(name))
