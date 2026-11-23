"""Job middleware."""


class WithoutOverlapping:
    """Run at most one refresh per entity at a time.

    A delivery that finds another refresh of the same entity running is put back on the queue for
    ``release_after`` seconds instead of running.
    """

    def __init__(self, store, release_after, expires_after):
        self.store = store
        self.release_after = release_after
        self.expires_after = expires_after

    def handle(self, job, command, next_):
        lock = self.store.lock(command.overlap_key(), self.expires_after)
        if lock.acquire():
            try:
                return next_(command)
            finally:
                lock.release()
        if self.release_after is not None:
            job.release(self.release_after)
        return None
