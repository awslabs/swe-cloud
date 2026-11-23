"""One output bucket of the sink: a single in-progress part file.

Lifecycle per attempt: either start a new part file, or restore the in-progress file from a
checkpointed S3Recoverable; then write records; at each checkpoint persist the in-progress file
and hand the recoverable to the job manager; when the job manager reports a checkpoint complete,
free the temporary state that is no longer needed.
"""
from __future__ import annotations

import logging
from typing import Dict, Optional

from .recoverable import S3Recoverable
from .writer import S3RecoverableOutputStream, S3RecoverableWriter

LOG = logging.getLogger("partsink.bucket")


class Bucket:
    def __init__(self, fs_writer: S3RecoverableWriter, part_key: str):
        self.fs_writer = fs_writer
        self.part_key = part_key
        self.in_progress: Optional[S3RecoverableOutputStream] = None
        self.resumables_per_checkpoint: Dict[int, S3Recoverable] = {}

    # -- start / restore --------------------------------------------------------------------

    def open_new_part_file(self) -> str:
        self.in_progress = self.fs_writer.open(self.part_key)
        return self.in_progress.upload.upload_id

    def restore_in_progress_file(self, resumable: S3Recoverable) -> None:
        self.in_progress = self.fs_writer.recover(resumable)

        if self.fs_writer.requires_cleanup_of_recoverable_state():
            self.fs_writer.cleanup_recoverable_state(resumable)

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

    def cleanup_outdated_resumables(self, checkpoint_id: int) -> None:
        for cid in sorted(c for c in self.resumables_per_checkpoint if c < checkpoint_id):
            recoverable = self.resumables_per_checkpoint.pop(cid)
            if self.fs_writer.cleanup_recoverable_state(recoverable):
                LOG.debug("deleted incomplete part %s of checkpoint %d", recoverable.tail_key, cid)
