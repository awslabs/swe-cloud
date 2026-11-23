"""Error codes and the exceptions the producer raises."""

from __future__ import annotations

NONE = 0
UNKNOWN_TOPIC_OR_PARTITION = 3
NOT_LEADER_OR_FOLLOWER = 6
REQUEST_TIMED_OUT = 7
COORDINATOR_LOAD_IN_PROGRESS = 14
COORDINATOR_NOT_AVAILABLE = 15
NOT_COORDINATOR = 16
NOT_ENOUGH_REPLICAS = 19
NOT_ENOUGH_REPLICAS_AFTER_APPEND = 20
OUT_OF_ORDER_SEQUENCE_NUMBER = 45
DUPLICATE_SEQUENCE_NUMBER = 46
INVALID_PRODUCER_EPOCH = 47
INVALID_TXN_STATE = 48
INVALID_PRODUCER_ID_MAPPING = 49
INVALID_TRANSACTION_TIMEOUT = 50
CONCURRENT_TRANSACTIONS = 51
TRANSACTIONAL_ID_AUTHORIZATION_FAILED = 53
UNKNOWN_PRODUCER_ID = 59
PRODUCER_FENCED = 90

NAMES = {
    0: "NONE",
    3: "UNKNOWN_TOPIC_OR_PARTITION",
    6: "NOT_LEADER_OR_FOLLOWER",
    7: "REQUEST_TIMED_OUT",
    14: "COORDINATOR_LOAD_IN_PROGRESS",
    15: "COORDINATOR_NOT_AVAILABLE",
    16: "NOT_COORDINATOR",
    19: "NOT_ENOUGH_REPLICAS",
    20: "NOT_ENOUGH_REPLICAS_AFTER_APPEND",
    45: "OUT_OF_ORDER_SEQUENCE_NUMBER",
    46: "DUPLICATE_SEQUENCE_NUMBER",
    47: "INVALID_PRODUCER_EPOCH",
    48: "INVALID_TXN_STATE",
    49: "INVALID_PRODUCER_ID_MAPPING",
    50: "INVALID_TRANSACTION_TIMEOUT",
    51: "CONCURRENT_TRANSACTIONS",
    53: "TRANSACTIONAL_ID_AUTHORIZATION_FAILED",
    59: "UNKNOWN_PRODUCER_ID",
    90: "PRODUCER_FENCED",
}

# The coordinator is moving, loading or busy with the previous transaction: retry the same request.
RETRIABLE_COORDINATOR = {COORDINATOR_LOAD_IN_PROGRESS, COORDINATOR_NOT_AVAILABLE, NOT_COORDINATOR,
                         CONCURRENT_TRANSACTIONS, REQUEST_TIMED_OUT}
# The leader refused the batch before appending it: resend the same bytes after a metadata refresh.
RETRIABLE_PRODUCE = {UNKNOWN_TOPIC_OR_PARTITION, NOT_LEADER_OR_FOLLOWER, REQUEST_TIMED_OUT,
                     NOT_ENOUGH_REPLICAS, NOT_ENOUGH_REPLICAS_AFTER_APPEND}
# A newer instance of the transactional id holds the epoch.
FENCED = {INVALID_PRODUCER_EPOCH, PRODUCER_FENCED}


def name(code: int) -> str:
    return NAMES.get(code, f"ERROR_{code}")


class ProducerError(Exception):
    """Base class. ``code`` is the broker error code when there is one."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class FatalProducerError(ProducerError):
    """The producer cannot be used any more; close it."""


class ProducerFencedError(FatalProducerError):
    """Another instance of this transactional id has taken over."""


class AbortableTransactionError(ProducerError):
    """The current transaction cannot commit. Abort it; the producer can be used afterwards."""


class DeliveryTimeoutError(AbortableTransactionError):
    """A record was not acknowledged within delivery_timeout_ms."""


def broker_error(operation: str, code: int) -> ProducerError:
    message = f"{operation} failed: {name(code)} ({code})"
    if code in FENCED:
        return ProducerFencedError(message, code)
    if code in (TRANSACTIONAL_ID_AUTHORIZATION_FAILED, INVALID_TRANSACTION_TIMEOUT):
        return FatalProducerError(message, code)
    return AbortableTransactionError(message, code)
