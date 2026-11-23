"""A thin object-store interface over one S3 bucket (versioning enabled).

Keys are dataset-relative paths such as `warehouse/orders/_versions/3.manifest`. Every write to a
key creates a new object version; `ObjectMeta.generation` is the S3 VersionId of the version a
call observed.
"""

from dataclasses import dataclass
from typing import Optional

from botocore.exceptions import ClientError

from .errors import ObjectNotFound, StrataError

_NOT_FOUND = {"NoSuchKey", "NotFound", "404", "NoSuchVersion"}


@dataclass
class ObjectMeta:
    path: str
    size: int
    generation: Optional[str]
    e_tag: Optional[str]


def _is_not_found(error):
    code = error.response.get("Error", {}).get("Code")
    status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in _NOT_FOUND or status == 404


class ObjectStore:
    def __init__(self, client, bucket):
        self.client = client
        self.bucket = bucket

    def _call(self, path, fn, **kwargs):
        try:
            return fn(Bucket=self.bucket, **kwargs)
        except ClientError as error:
            if _is_not_found(error):
                raise ObjectNotFound(f"Object at location {path} not found: {error}") from error
            raise StrataError(f"object store request for {path} failed: {error}") from error

    def put(self, path, data):
        response = self._call(path, self.client.put_object, Key=path, Body=data)
        return ObjectMeta(path, len(data), response.get("VersionId"), response.get("ETag"))

    def get(self, path):
        response = self._call(path, self.client.get_object, Key=path)
        return response["Body"].read()

    def head(self, path):
        response = self._call(path, self.client.head_object, Key=path)
        return ObjectMeta(
            path, response["ContentLength"], response.get("VersionId"), response.get("ETag")
        )

    def copy(self, source, destination):
        """Server-side copy. Raises ObjectNotFound when `source` does not exist."""
        self._call(
            source,
            self.client.copy_object,
            Key=destination,
            CopySource={"Bucket": self.bucket, "Key": source},
        )

    def delete(self, path):
        self._call(path, self.client.delete_object, Key=path)

    def list(self, prefix):
        paginator = self.client.get_paginator("list_objects_v2")
        keys = []
        try:
            for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
                keys.extend(item["Key"] for item in page.get("Contents", []))
        except ClientError as error:
            raise StrataError(f"listing {prefix} failed: {error}") from error
        return keys
