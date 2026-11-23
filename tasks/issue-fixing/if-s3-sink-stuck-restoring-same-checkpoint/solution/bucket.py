"""One output bucket of the sink: a single in-progress part file.

Lifecycle per attempt: either start a new part file, or restore the in-progress file from a
checkpointed S3Recoverable; then write records; at each checkpoint persist the in-progress file
and hand the recoverable to the job manager; when the job manager reports a checkpoint complete,
free the temporary state that is no longer needed.

Temporary state is freed by reachability: a tail object or a multipart upload of this job is
kept while any manifest under chk/ or savepoints/ names it, and while it belongs to a snapshot of
this attempt that the job manager has not decided yet. Everything else under the job's prefix,
including what earlier attempts left behind, is deleted or aborted. Nothing is freed on restore.

Only an attempt the job manager is still talking to frees state (on COMPLETE, and on END when the
job has finished). A cancelled attempt may have been superseded while it was cut off: the running
attempt's new upload and undecided snapshots are named by no manifest yet, so it frees nothing.
"""
from __future__ import annotations

import json
import logging
from typing import Dict, Optional, Set, Tuple

import botocore.exceptions

from .recoverable import S3Recoverable
from .writer import S3RecoverableOutputStream, S3RecoverableWriter, incomplete_part_object_name_prefix

LOG = logging.getLogger("partsink.bucket")


class ManifestUnreadable(Exception):
    pass


class Bucket:
    def __init__(self, fs_writer: S3RecoverableWriter, part_key: str):
        self.fs_writer = fs_writer
        self.part_key = part_key
        self.in_progress: Optional[S3RecoverableOutputStream] = None
        self.resumables_per_checkpoint: Dict[int, S3Recoverable] = {}
        self.job_prefix = part_key.split("/out/", 1)[0] + "/"          # jobs/<job>/
        self.tmp_prefix = incomplete_part_object_name_prefix(part_key)

    # -- start / restore --------------------------------------------------------------------

    def open_new_part_file(self) -> str:
        self.in_progress = self.fs_writer.open(self.part_key)
        return self.in_progress.upload.upload_id

    def restore_in_progress_file(self, resumable: S3Recoverable) -> None:
        # The manifest we restore from may be restored again (open failure, a crash before the
        # next checkpoint, a manual restore): its state is freed only once no manifest names it.
        self.in_progress = self.fs_writer.recover(resumable)

    # -- data -------------------------------------------------------------------------------

    def write(self, record: bytes) -> None:
        self.in_progress.write_record(record)

    @property
    def offset(self) -> int:
        return self.in_progress.offset

    # -- checkpointing ----------------------------------------------------------------------

    def on_reception_of_checkpoint(self, checkpoint_id: int) -> S3Recoverable:
        resumable = self.in_progress.persist()
        if self.fs_writer.requires_cleanup_of_recoverable_state():
            self.resumables_per_checkpoint[checkpoint_id] = resumable
        return resumable

    def on_successful_completion_of_checkpoint(self, checkpoint_id: int) -> None:
        self.cleanup_outdated_resumables(checkpoint_id)

    def on_end_of_job(self) -> None:
        # The job finished and its final checkpoint is written: every snapshot is decided, and
        # the notification for the last one may never come.
        self.resumables_per_checkpoint.clear()
        self.cleanup_outdated_resumables(None)

    def cleanup_outdated_resumables(self, checkpoint_id: Optional[int]) -> None:
        # Snapshots up to this checkpoint are decided: their manifest exists or never will.
        # Later ones may still be completed, so their tails stay.
        for cid in [c for c in self.resumables_per_checkpoint if checkpoint_id is None or c <= checkpoint_id]:
            del self.resumables_per_checkpoint[cid]
        pending = {r.tail_key for r in self.resumables_per_checkpoint.values() if r.tail_key}
        try:
            tails, uploads = self.referenced_state()
        except ManifestUnreadable as e:
            LOG.warning("skipping cleanup after checkpoint %d: %s", checkpoint_id, e)
            return
        live = self.in_progress.upload.upload_id if self.in_progress and checkpoint_id is not None else None

        s3, bucket = self.fs_writer.s3, self.fs_writer.bucket
        for key in self._keys(self.tmp_prefix):
            if key not in tails and key not in pending:
                s3.delete_object(Bucket=bucket, Key=key)
                LOG.info("deleted unreferenced tail %s", key)
        for key, upload_id in self._uploads():
            if upload_id not in uploads and upload_id != live:
                try:
                    s3.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
                    LOG.info("aborted unreferenced upload %s on %s", upload_id, key)
                except botocore.exceptions.ClientError as e:
                    if e.response["Error"]["Code"] != "NoSuchUpload":
                        raise

    # -- durable references -----------------------------------------------------------------

    def referenced_state(self) -> Tuple[Set[str], Set[str]]:
        s3, bucket = self.fs_writer.s3, self.fs_writer.bucket
        tails: Set[str] = set()
        uploads: Set[str] = set()
        for prefix in ("chk/", "savepoints/"):
            for key in self._keys(self.job_prefix + prefix):
                if not key.endswith(".json"):
                    continue
                try:
                    state = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())["state"]
                except botocore.exceptions.ClientError as e:
                    if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                        continue                       # retired between list and get
                    raise
                except (ValueError, KeyError, TypeError) as e:
                    raise ManifestUnreadable(f"{key}: {e}")
                if state.get("tail_key"):
                    tails.add(state["tail_key"])
                if state.get("upload_id"):
                    uploads.add(state["upload_id"])
        return tails, uploads

    def _keys(self, prefix: str):
        s3, bucket = self.fs_writer.s3, self.fs_writer.bucket
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                if obj["Key"].startswith(prefix):
                    yield obj["Key"]

    def _uploads(self):
        s3, bucket = self.fs_writer.s3, self.fs_writer.bucket
        found = []
        kwargs = {"Bucket": bucket, "Prefix": self.job_prefix}
        while True:
            page = s3.list_multipart_uploads(**kwargs)
            for u in page.get("Uploads", []):
                if u["Key"].startswith(self.job_prefix):    # the prefix filter is not trusted
                    found.append((u["Key"], u["UploadId"]))
            if not page.get("IsTruncated"):
                return found
            kwargs.update(KeyMarker=page.get("NextKeyMarker", ""), UploadIdMarker=page.get("NextUploadIdMarker", ""))
