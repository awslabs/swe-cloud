"""Run one delivery of a refresh job through its middleware and the refresh command."""

import shlex
import subprocess

from .jobs import RefreshEntity
from .locks import LockStore
from .middleware import WithoutOverlapping
from .unique import UniqueLock


class RefreshFailed(Exception):
    pass


class CallQueuedHandler:
    def __init__(self, cfg, dynamodb):
        self.cfg = cfg
        self.unique = UniqueLock(LockStore(dynamodb, cfg.unique_table))
        self.middleware = [
            WithoutOverlapping(LockStore(dynamodb, cfg.overlap_table),
                               release_after=cfg.release_after,
                               expires_after=cfg.overlap_expires),
        ]

    def call(self, job):
        command = RefreshEntity.from_body(job.body)
        self.run_through_middleware(job, command)
        if not job.is_deleted_or_released():
            job.delete()

    def run_through_middleware(self, job, command):
        lock_released = False

        def destination(cmd):
            nonlocal lock_released
            if job.attempts() <= 1:
                self.unique.release(cmd)
                lock_released = True
            return self.run_refresh(job, cmd)

        try:
            return self._pipeline(job, command, destination)
        finally:
            if not lock_released and not job.released and job.attempts() <= 1:
                self.unique.release(command)

    def _pipeline(self, job, command, destination):
        def stage(index):
            if index == len(self.middleware):
                return destination
            return lambda cmd: self.middleware[index].handle(job, cmd, stage(index + 1))
        return stage(0)(command)

    def run_refresh(self, job, command):
        argv = shlex.split(self.cfg.refresh_cmd) + [
            "--entity", command.entity_id,
            "--message-id", job.message_id,
            "--attempt", str(job.attempts()),
        ]
        result = subprocess.run(argv)
        if result.returncode != 0:
            raise RefreshFailed(f"refresh command exited {result.returncode}")
