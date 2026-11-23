"""Error types. Each maps to the exit code `stratactl` uses for it."""


class StrataError(Exception):
    exit_code = 1


class NotFound(StrataError):
    exit_code = 4


class CorruptManifest(StrataError):
    """The object store and the version index disagree about a manifest."""

    exit_code = 2


class CommitConflict(StrataError):
    """Another writer holds the version this commit tried to claim."""

    exit_code = 3


class ObjectNotFound(StrataError):
    """An object-store key (or the copy source) does not exist."""

    exit_code = 4


class IndexWriteFailed(StrataError):
    """A conditional write to the version index did not apply.

    `condition_failed` is True when DynamoDB rejected the write's condition, as opposed to the
    request failing for any other reason (throttling, 5xx, timeouts).
    """

    def __init__(self, message, condition_failed=False):
        super().__init__(message)
        self.condition_failed = condition_failed
