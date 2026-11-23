"""The per-entity dedup lock taken at dispatch and given up when the refresh starts."""

from .locks import FOREVER, LockStore

# dispatch builds the job (``dispatched_at``) and then takes the lock (``expires_at`` = whole
# seconds of that moment + lifetime); creating the client in between can cross a second.
TAKEN_SLACK = 2


class UniqueLock:
    def __init__(self, store: LockStore):
        self.store = store

    def acquire(self, job):
        return self.store.lock(job.unique_key(), job.unique_for).acquire()

    def taken_by(self, job, held):
        """Whether the lock item ``held`` is the one this job's own dispatch wrote.

        The message carries no owner, so the item is recognised by when it was taken: a later
        dispatch can only take the key after this job's lock was released or had lapsed.
        """
        seconds = job.unique_for if job.unique_for > 0 else FOREVER
        taken_at = held["expires_at"] - seconds
        return 0 <= taken_at - int(job.dispatched_at) <= TAKEN_SLACK

    def release(self, job):
        """Give up the lock this job's dispatch took, if it still holds it."""
        held = self.store.get(job.unique_key())
        if held is None or not self.taken_by(job, held):
            return False
        # Conditional on the owner seen, so a dispatch that took the key meanwhile keeps it.
        return self.store.restore(job.unique_key(), held["owner"]).release()
