"""The per-entity dedup lock taken at dispatch and given up when the refresh starts."""

from .locks import LockStore


class UniqueLock:
    def __init__(self, store: LockStore):
        self.store = store

    def acquire(self, job):
        return self.store.lock(job.unique_key(), job.unique_for).acquire()

    def release(self, job):
        self.store.lock(job.unique_key()).force_release()
