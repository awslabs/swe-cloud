"""A transactional Kafka producer: a small synchronous port of the Java client's TransactionManager and Sender.

One instance owns one ``transactional.id``. The life cycle is the Java client's:

    init_transactions()                       once, before anything else
    begin_transaction()
    send(...) ... send(...)                   each call blocks until the record is acknowledged
    commit_transaction() | abort_transaction()

``send`` raises ``AbortableTransactionError`` (including ``DeliveryTimeoutError``) when the current
transaction can no longer commit; the caller aborts and may start a new transaction. It raises
``FatalProducerError`` / ``ProducerFencedError`` when the producer must be closed.

Records are sent one Produce request at a time and each request waits for its own response, so at
most one request per partition is in flight.
"""

from __future__ import annotations

import enum
import logging
import time

from . import errors
from . import protocol as p
from .errors import (AbortableTransactionError, DeliveryTimeoutError, FatalProducerError,
                     ProducerError, ProducerFencedError)

log = logging.getLogger("txnclient.producer")


class State(enum.Enum):
    UNINITIALIZED = "UNINITIALIZED"
    INITIALIZING = "INITIALIZING"
    READY = "READY"
    IN_TRANSACTION = "IN_TRANSACTION"
    COMMITTING_TRANSACTION = "COMMITTING_TRANSACTION"
    ABORTING_TRANSACTION = "ABORTING_TRANSACTION"
    ABORTABLE_ERROR = "ABORTABLE_ERROR"
    FATAL_ERROR = "FATAL_ERROR"


def parse_bootstrap(bootstrap: str) -> list[tuple[str, int]]:
    out = []
    for item in bootstrap.split(","):
        host, _, port = item.strip().rpartition(":")
        out.append((host, int(port)))
    return out


class TransactionalProducer:
    def __init__(self, bootstrap: str, transactional_id: str, client_id: str, *,
                 transaction_timeout_ms: int = 60000, delivery_timeout_ms: int = 5000,
                 request_timeout_ms: int = 30000, max_block_ms: int = 60000, retry_backoff_ms: int = 100):
        self.bootstrap = parse_bootstrap(bootstrap)
        self.transactional_id = transactional_id
        self.client_id = client_id
        self.transaction_timeout_ms = transaction_timeout_ms
        self.delivery_timeout_ms = delivery_timeout_ms
        self.request_timeout_ms = request_timeout_ms
        self.max_block_ms = max_block_ms
        self.retry_backoff_ms = retry_backoff_ms

        self.producer_id = p.NO_PRODUCER_ID
        self.producer_epoch = p.NO_PRODUCER_EPOCH
        self.state = State.UNINITIALIZED
        self.last_error: ProducerError | None = None

        self._next_sequence: dict[tuple[str, int], int] = {}
        self._partitions_in_txn: set[tuple[str, int]] = set()
        self._epoch_bump_required = False
        self._coordinator_supports_epoch_bump = False

        self._brokers: dict[int, p.BrokerInfo] = {}
        self._leaders: dict[tuple[str, int], int] = {}
        self._coordinator: p.BrokerInfo | None = None
        self._connections: dict[tuple[str, int, str], p.BrokerConnection] = {}

    # ── public API ─────────────────────────────────────────────────────────────────

    def init_transactions(self) -> None:
        self._require(State.UNINITIALIZED, "init_transactions")
        self.state = State.INITIALIZING
        try:
            self._probe_api_versions()
            producer_id, producer_epoch = self._init_producer_id()
        except ProducerError as exc:
            self._transition_to_fatal(exc)
            raise
        self._set_producer_id_and_epoch(producer_id, producer_epoch)
        self.state = State.READY
        log.info("initialized transactional id %s: producer id %d, epoch %d",
                 self.transactional_id, self.producer_id, self.producer_epoch)

    def begin_transaction(self) -> None:
        self._raise_if_fatal()
        self._require(State.READY, "begin_transaction")
        self.state = State.IN_TRANSACTION

    def send(self, topic: str, partition: int, key: bytes | None, value: bytes | None) -> int:
        """Append one record to the open transaction; returns its offset once acknowledged."""
        self._raise_if_fatal()
        if self.state == State.ABORTABLE_ERROR:
            raise AbortableTransactionError(f"transaction must be aborted: {self.last_error}")
        self._require(State.IN_TRANSACTION, "send")
        tp = (topic, partition)
        if tp not in self._partitions_in_txn:
            self._add_partition_to_transaction(tp)

        sequence = self._next_sequence.get(tp, 0)
        records = p.encode_record_batch([(key, value)], producer_id=self.producer_id,
                                        producer_epoch=self.producer_epoch, base_sequence=sequence,
                                        transactional=True)
        body = p.produce_request(self.transactional_id, topic, partition, records,
                                 acks=-1, timeout_ms=self.delivery_timeout_ms)
        self._next_sequence[tp] = sequence + 1

        deadline = time.monotonic() + self.delivery_timeout_ms / 1000
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise self._fail_send(tp, DeliveryTimeoutError(
                    f"record for {topic}-{partition} (sequence {sequence}) not acknowledged within "
                    f"{self.delivery_timeout_ms} ms"))
            try:
                conn = self._leader_connection(tp)
                response = conn.request(p.PRODUCE, body, remaining)
            except p.RequestTimedOut:
                raise self._fail_send(tp, DeliveryTimeoutError(
                    f"record for {topic}-{partition} (sequence {sequence}) not acknowledged within "
                    f"{self.delivery_timeout_ms} ms")) from None
            except OSError as exc:
                log.warning("produce to %s-%d: connection error %s; retrying", topic, partition, exc)
                self._forget_leader(tp)
                self._backoff()
                continue
            error, offset = p.produce_response(response).get(tp, (errors.UNKNOWN_TOPIC_OR_PARTITION, -1))
            if error in (errors.NONE, errors.DUPLICATE_SEQUENCE_NUMBER):
                return offset
            if error in errors.RETRIABLE_PRODUCE:
                log.warning("produce to %s-%d: %s; retrying", topic, partition, errors.name(error))
                self._forget_leader(tp)
                self._backoff()
                continue
            raise self._fail_send(tp, errors.broker_error(f"produce to {topic}-{partition}", error))

    def commit_transaction(self) -> None:
        self._raise_if_fatal()
        if self.state == State.ABORTABLE_ERROR:
            raise AbortableTransactionError(f"cannot commit, transaction must be aborted: {self.last_error}")
        self._require(State.IN_TRANSACTION, "commit_transaction")
        self.state = State.COMMITTING_TRANSACTION
        if self._partitions_in_txn:
            try:
                self._end_txn(commit=True)
            except FatalProducerError as exc:
                self._transition_to_fatal(exc)
                raise
            except ProducerError as exc:
                self._transition_to_abortable(exc)
                raise
        self._complete_transaction()

    def abort_transaction(self) -> None:
        self._raise_if_fatal()
        if self.state not in (State.IN_TRANSACTION, State.ABORTABLE_ERROR, State.ABORTING_TRANSACTION):
            raise FatalProducerError(f"abort_transaction called in state {self.state.value}")
        self.state = State.ABORTING_TRANSACTION
        try:
            if self._partitions_in_txn:
                self._end_txn(commit=False)
            if self._epoch_bump_required:
                self._bump_epoch()
        except ProducerError as exc:
            self._transition_to_fatal(exc)
            raise
        self._complete_transaction()

    def close(self) -> None:
        for conn in self._connections.values():
            conn.close()
        self._connections.clear()

    # ── state ──────────────────────────────────────────────────────────────────────

    def _require(self, state: State, operation: str) -> None:
        if self.state != state:
            raise FatalProducerError(f"{operation} called in state {self.state.value}")

    def _raise_if_fatal(self) -> None:
        if self.state == State.FATAL_ERROR:
            assert self.last_error is not None
            raise self.last_error

    def _transition_to_fatal(self, exc: ProducerError) -> None:
        if not isinstance(exc, FatalProducerError):
            exc = FatalProducerError(str(exc), exc.code)
        self.last_error = exc
        self.state = State.FATAL_ERROR

    def _transition_to_abortable(self, exc: ProducerError) -> None:
        self.last_error = exc
        self.state = State.ABORTABLE_ERROR

    def _maybe_transition_to_error_state(self, exc: ProducerError) -> None:
        if isinstance(exc, FatalProducerError):
            self._transition_to_fatal(exc)
            return
        if exc.code in (errors.OUT_OF_ORDER_SEQUENCE_NUMBER, errors.UNKNOWN_PRODUCER_ID,
                        errors.INVALID_PRODUCER_ID_MAPPING):
            # The partition's sequence state for this producer no longer matches ours; a new epoch
            # restarts the sequences at 0.
            if not self._coordinator_supports_epoch_bump:
                self._transition_to_fatal(exc)
                return
            self._epoch_bump_required = True
        self._transition_to_abortable(exc)

    def _fail_send(self, tp: tuple[str, int], exc: ProducerError) -> ProducerError:
        log.warning("send to %s-%d failed: %s", tp[0], tp[1], exc)
        self._maybe_transition_to_error_state(exc)
        if self.state == State.FATAL_ERROR:
            assert self.last_error is not None
            return self.last_error
        return exc

    def _complete_transaction(self) -> None:
        self._partitions_in_txn.clear()
        self._epoch_bump_required = False
        self.last_error = None
        self.state = State.READY

    def _set_producer_id_and_epoch(self, producer_id: int, producer_epoch: int) -> None:
        self.producer_id = producer_id
        self.producer_epoch = producer_epoch
        self._next_sequence.clear()

    def _bump_epoch(self) -> None:
        old = (self.producer_id, self.producer_epoch)
        producer_id, producer_epoch = self._init_producer_id()
        self._set_producer_id_and_epoch(producer_id, producer_epoch)
        log.info("bumped producer %s -> (%d, %d)", old, producer_id, producer_epoch)

    # ── transaction coordinator requests ───────────────────────────────────────────

    def _init_producer_id(self) -> tuple[int, int]:
        body = p.init_producer_id_request(self.transactional_id, self.transaction_timeout_ms)
        error, producer_id, producer_epoch = self._coordinator_request(
            p.INIT_PRODUCER_ID, body, p.init_producer_id_response, lambda r: r[0], "InitProducerId")
        return producer_id, producer_epoch

    def _add_partition_to_transaction(self, tp: tuple[str, int]) -> None:
        body = p.add_partitions_request(self.transactional_id, self.producer_id, self.producer_epoch,
                                        {tp[0]: [tp[1]]})
        try:
            self._coordinator_request(p.ADD_PARTITIONS_TO_TXN, body, p.add_partitions_response,
                                      lambda r: r.get(tp, errors.UNKNOWN_TOPIC_OR_PARTITION), "AddPartitionsToTxn")
        except ProducerError as exc:
            self._maybe_transition_to_error_state(exc)
            raise
        self._partitions_in_txn.add(tp)

    def _end_txn(self, commit: bool) -> None:
        body = p.end_txn_request(self.transactional_id, self.producer_id, self.producer_epoch, commit)
        self._coordinator_request(p.END_TXN, body, p.end_txn_response, lambda r: r,
                                  "EndTxn(" + ("COMMIT" if commit else "ABORT") + ")")

    def _coordinator_request(self, api_key: int, body: bytes, decode, error_of, operation: str):
        """Send a request to the transaction coordinator, retrying while it is busy or moving."""
        deadline = time.monotonic() + self.max_block_ms / 1000
        while True:
            if time.monotonic() > deadline:
                raise FatalProducerError(f"{operation}: no usable response within {self.max_block_ms} ms")
            try:
                conn = self._coordinator_connection()
                result = decode(conn.request(api_key, body, self.request_timeout_ms / 1000))
            except (OSError, p.RequestTimedOut) as exc:
                log.warning("%s: %s; retrying", operation, exc)
                self._coordinator = None
                self._backoff()
                continue
            error = error_of(result)
            if error == errors.NONE:
                return result
            if error in errors.RETRIABLE_COORDINATOR:
                log.info("%s: %s; retrying", operation, errors.name(error))
                if error in (errors.NOT_COORDINATOR, errors.COORDINATOR_NOT_AVAILABLE):
                    self._coordinator = None
                self._backoff()
                continue
            raise errors.broker_error(operation, error)

    # ── connections and metadata ───────────────────────────────────────────────────

    def _backoff(self) -> None:
        time.sleep(self.retry_backoff_ms / 1000)

    def _connection(self, host: str, port: int, purpose: str) -> p.BrokerConnection:
        key = (host, port, purpose)
        conn = self._connections.get(key)
        if conn is None or conn.closed:
            conn = p.BrokerConnection(host, port, self.client_id)
            self._connections[key] = conn
        return conn

    def _any_connection(self) -> p.BrokerConnection:
        last: Exception | None = None
        for host, port in self.bootstrap:
            try:
                return self._connection(host, port, "bootstrap")
            except OSError as exc:
                last = exc
        raise FatalProducerError(f"no bootstrap broker reachable: {last}")

    def _probe_api_versions(self) -> None:
        error, ranges = p.api_versions_response(
            self._any_connection().request(p.API_VERSIONS, p.api_versions_request(), self.request_timeout_ms / 1000))
        if error:
            raise FatalProducerError(f"ApiVersions failed: {errors.name(error)}", error)
        low, high = ranges.get(p.INIT_PRODUCER_ID, (0, -1))
        self._coordinator_supports_epoch_bump = high >= 3

    def _coordinator_connection(self) -> p.BrokerConnection:
        if self._coordinator is None:
            r = self._any_connection().request(p.FIND_COORDINATOR, p.find_coordinator_request(self.transactional_id),
                                               self.request_timeout_ms / 1000)
            error, broker = p.find_coordinator_response(r)
            if error or broker is None:
                raise OSError(f"FindCoordinator: {errors.name(error)}")
            self._coordinator = broker
        return self._connection(self._coordinator.host, self._coordinator.port, "coordinator")

    def _refresh_metadata(self, topic: str) -> None:
        r = self._any_connection().request(p.METADATA, p.metadata_request([topic]), self.request_timeout_ms / 1000)
        brokers, topics = p.metadata_response(r)
        self._brokers.update(brokers)
        info = topics.get(topic)
        if info is None or info.error_code:
            raise OSError(f"metadata for {topic}: {errors.name(info.error_code if info else -1)}")
        for part in info.partitions:
            if part.error_code == 0 and part.leader >= 0:
                self._leaders[(topic, part.partition)] = part.leader

    def _leader_connection(self, tp: tuple[str, int]) -> p.BrokerConnection:
        if tp not in self._leaders:
            self._refresh_metadata(tp[0])
        leader = self._brokers.get(self._leaders.get(tp, -1))
        if leader is None:
            raise OSError(f"no leader for {tp[0]}-{tp[1]}")
        return self._connection(leader.host, leader.port, "produce")

    def _forget_leader(self, tp: tuple[str, int]) -> None:
        self._leaders.pop(tp, None)
