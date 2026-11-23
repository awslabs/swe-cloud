"""S3 recoverable writer.

An in-progress part file is one S3 multipart upload. Bytes are buffered locally and shipped as
parts of at least MIN_PART_BYTES (S3 rejects smaller non-last parts). At a checkpoint the buffered
remainder, which is smaller than a part, is uploaded as an independent object (the "tail") and the
returned S3Recoverable names the upload, its completed parts and the tail. Restoring downloads
the tail into the local buffer and continues the same upload.
"""
from __future__ import annotations

import logging
import uuid
from typing import List, Optional

import botocore.exceptions

from .recoverable import S3Recoverable

LOG = logging.getLogger("partsink.writer")

MIN_PART_BYTES = 5 * 1024 * 1024


class RecoverableStateMissing(FileNotFoundError):
    """The S3 state a recoverable names is not there (or not the recorded size)."""


def incomplete_part_object_name_prefix(object_name: str) -> str:
    """'jobs/j/out/part-0' -> 'jobs/j/out/_part-0_tmp_'."""
    slash = object_name.rfind("/")
    parent, child = (object_name[: slash + 1], object_name[slash + 1:]) if slash >= 0 else ("", object_name)
    return parent + ("_" if child else "") + child + "_tmp_"


class RecoverableMultiPartUpload:
    def __init__(self, s3, bucket: str, key: str, upload_id: str,
                 parts: Optional[List[dict]] = None, bytes_in_parts: int = 0):
        self.s3 = s3
        self.bucket = bucket
        self.key = key
        self.upload_id = upload_id
        self.parts: List[dict] = list(parts or [])
        self.bytes_in_parts = bytes_in_parts
        self.tmp_prefix = incomplete_part_object_name_prefix(key)

    def upload_part(self, data: bytes) -> None:
        number = len(self.parts) + 1
        etag = self.s3.upload_part(Bucket=self.bucket, Key=self.key, UploadId=self.upload_id,
                                   PartNumber=number, Body=data)["ETag"]
        self.parts.append({"PartNumber": number, "ETag": etag, "Size": len(data)})
        self.bytes_in_parts += len(data)
        LOG.debug("uploaded part %d (%d bytes) of %s", number, len(data), self.key)

    def snapshot_and_get_recoverable(self, tail: bytes, offset: int) -> S3Recoverable:
        tail_key = None
        if tail:
            tail_key = self.tmp_prefix + str(uuid.uuid4())
            self.s3.put_object(Bucket=self.bucket, Key=tail_key, Body=tail)
        return S3Recoverable(key=self.key, upload_id=self.upload_id,
                             parts=[dict(p) for p in self.parts],
                             bytes_in_parts=self.bytes_in_parts,
                             tail_key=tail_key, tail_len=len(tail), offset=offset)


class S3RecoverableOutputStream:
    """Local buffer in front of a RecoverableMultiPartUpload."""

    def __init__(self, upload: RecoverableMultiPartUpload, buffered: bytes = b"", offset: int = 0,
                 part_bytes: int = MIN_PART_BYTES):
        self.upload = upload
        self.buffer = bytearray(buffered)
        self.offset = offset
        self.part_bytes = part_bytes

    def write_record(self, record: bytes) -> None:
        self.buffer += record
        self.offset += 1
        if len(self.buffer) >= self.part_bytes:
            data = bytes(self.buffer[: self.part_bytes])
            del self.buffer[: self.part_bytes]
            self.upload.upload_part(data)

    def persist(self) -> S3Recoverable:
        return self.upload.snapshot_and_get_recoverable(bytes(self.buffer), self.offset)


class S3RecoverableWriter:
    def __init__(self, s3, bucket: str):
        self.s3 = s3
        self.bucket = bucket

    def open(self, key: str) -> S3RecoverableOutputStream:
        upload_id = self.s3.create_multipart_upload(Bucket=self.bucket, Key=key)["UploadId"]
        return S3RecoverableOutputStream(RecoverableMultiPartUpload(self.s3, self.bucket, key, upload_id))

    def recover(self, r: S3Recoverable) -> S3RecoverableOutputStream:
        tail = b""
        if r.tail_key is not None:
            try:
                tail = self.s3.get_object(Bucket=self.bucket, Key=r.tail_key)["Body"].read()
            except botocore.exceptions.ClientError as e:
                if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                    raise RecoverableStateMissing(f"tail object {r.tail_key} is gone (NoSuchKey)") from e
                raise
            if len(tail) != r.tail_len:
                raise RecoverableStateMissing(
                    f"tail object {r.tail_key} has {len(tail)} bytes, expected {r.tail_len}")
        upload = RecoverableMultiPartUpload(self.s3, self.bucket, r.key, r.upload_id,
                                            r.parts, r.bytes_in_parts)
        return S3RecoverableOutputStream(upload, tail, r.offset)

    def requires_cleanup_of_recoverable_state(self) -> bool:
        return True

    def cleanup_recoverable_state(self, r: S3Recoverable) -> bool:
        """Free the temporary state of a recoverable.

        Must not raise when the state is already gone; returns False in that case (already
        deleted, or never created).
        """
        if r.tail_key is None:
            return False
        try:
            self.s3.head_object(Bucket=self.bucket, Key=r.tail_key)
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return False
            raise
        self.s3.delete_object(Bucket=self.bucket, Key=r.tail_key)
        return True
