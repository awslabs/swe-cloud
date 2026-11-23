"""Kafka wire protocol: framing, the request/response codecs the ledger writer uses, record batches.

Only what a single-partition transactional writer and its committed-log reader need is here:

    ApiVersions v0, Metadata v1, FindCoordinator v1, InitProducerId v3, AddPartitionsToTxn v1,
    Produce v7, EndTxn v1, ListOffsets v2, Fetch v4

Every function in this module is a pure codec or a blocking socket helper; nothing here decides
what to do with an error code. ``producer.py`` owns that.
"""

from __future__ import annotations

import socket
import struct
import time
from dataclasses import dataclass, field

# api keys
PRODUCE = 0
FETCH = 1
LIST_OFFSETS = 2
METADATA = 3
FIND_COORDINATOR = 10
API_VERSIONS = 18
INIT_PRODUCER_ID = 22
ADD_PARTITIONS_TO_TXN = 24
END_TXN = 26

VERSIONS = {
    API_VERSIONS: 0,
    METADATA: 1,
    FIND_COORDINATOR: 1,
    INIT_PRODUCER_ID: 3,
    ADD_PARTITIONS_TO_TXN: 1,
    PRODUCE: 7,
    END_TXN: 1,
    LIST_OFFSETS: 2,
    FETCH: 4,
}
FLEXIBLE = {INIT_PRODUCER_ID}  # request header v2 / response header v1 at the versions above

NO_PRODUCER_ID = -1
NO_PRODUCER_EPOCH = -1
COORDINATOR_TRANSACTION = 1
LATEST = -1
EARLIEST = -2
READ_UNCOMMITTED = 0
READ_COMMITTED = 1

ATTR_TRANSACTIONAL = 0x10
ATTR_CONTROL = 0x20


class ConnectionClosed(OSError):
    """The broker connection was closed (by either side) before a full response arrived."""


class RequestTimedOut(TimeoutError):
    """No response within the caller's deadline. The request may or may not have been processed."""


# ── primitive encoders ─────────────────────────────────────────────────────────────


def i8(v: int) -> bytes:
    return struct.pack(">b", v)


def i16(v: int) -> bytes:
    return struct.pack(">h", v)


def i32(v: int) -> bytes:
    return struct.pack(">i", v)


def i64(v: int) -> bytes:
    return struct.pack(">q", v)


def string(v: str | None) -> bytes:
    if v is None:
        return i16(-1)
    raw = v.encode()
    return i16(len(raw)) + raw


def nbytes(v: bytes | None) -> bytes:
    if v is None:
        return i32(-1)
    return i32(len(v)) + v


def array(items: list[bytes]) -> bytes:
    return i32(len(items)) + b"".join(items)


def uvarint(v: int) -> bytes:
    out = bytearray()
    while True:
        byte = v & 0x7F
        v >>= 7
        if v:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def varint(v: int) -> bytes:
    return uvarint((v << 1) ^ (v >> 63))


def compact_string(v: str | None) -> bytes:
    if v is None:
        return uvarint(0)
    raw = v.encode()
    return uvarint(len(raw) + 1) + raw


TAGS_NONE = uvarint(0)


class Reader:
    """Sequential decoder over one response body."""

    def __init__(self, data: bytes, offset: int = 0):
        self.data = data
        self.pos = offset

    def _unpack(self, fmt: str, size: int):
        (value,) = struct.unpack_from(fmt, self.data, self.pos)
        self.pos += size
        return value

    def i8(self) -> int:
        return self._unpack(">b", 1)

    def i16(self) -> int:
        return self._unpack(">h", 2)

    def i32(self) -> int:
        return self._unpack(">i", 4)

    def u32(self) -> int:
        return self._unpack(">I", 4)

    def i64(self) -> int:
        return self._unpack(">q", 8)

    def boolean(self) -> bool:
        return self.i8() != 0

    def raw(self, n: int) -> bytes:
        chunk = self.data[self.pos:self.pos + n]
        if len(chunk) != n:
            raise ValueError("truncated response")
        self.pos += n
        return chunk

    def string(self) -> str | None:
        n = self.i16()
        return None if n < 0 else self.raw(n).decode()

    def nbytes(self) -> bytes | None:
        n = self.i32()
        return None if n < 0 else self.raw(n)

    def uvarint(self) -> int:
        shift = result = 0
        while True:
            byte = self.data[self.pos]
            self.pos += 1
            result |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return result
            shift += 7

    def varint(self) -> int:
        v = self.uvarint()
        return (v >> 1) ^ -(v & 1)

    def compact_string(self) -> str | None:
        n = self.uvarint()
        return None if n == 0 else self.raw(n - 1).decode()

    def tags(self) -> None:
        for _ in range(self.uvarint()):
            self.uvarint()
            self.raw(self.uvarint())

    def array(self, item):
        n = self.i32()
        return [] if n < 0 else [item(self) for _ in range(n)]


# ── crc32c (Castagnoli), for record batch v2 ───────────────────────────────────────


def _crc_table() -> list[int]:
    table = []
    for n in range(256):
        c = n
        for _ in range(8):
            c = (c >> 1) ^ 0x82F63B78 if c & 1 else c >> 1
        table.append(c)
    return table


_CRC_TABLE = _crc_table()


def crc32c(data: bytes) -> int:
    c = 0xFFFFFFFF
    for b in data:
        c = _CRC_TABLE[(c ^ b) & 0xFF] ^ (c >> 8)
    return c ^ 0xFFFFFFFF


# ── record batches (magic 2) ───────────────────────────────────────────────────────


@dataclass
class Record:
    offset: int
    key: bytes | None
    value: bytes | None
    timestamp: int


@dataclass
class RecordBatch:
    base_offset: int
    last_offset: int
    producer_id: int
    producer_epoch: int
    base_sequence: int
    attributes: int
    records: list[Record] = field(default_factory=list)

    @property
    def transactional(self) -> bool:
        return bool(self.attributes & ATTR_TRANSACTIONAL)

    @property
    def control(self) -> bool:
        return bool(self.attributes & ATTR_CONTROL)

    def control_type(self) -> int | None:
        """0 for an abort marker, 1 for a commit marker, None for a data batch."""
        if not self.control or not self.records or not self.records[0].key:
            return None
        return struct.unpack(">hh", self.records[0].key[:4])[1]


def encode_record_batch(records: list[tuple[bytes | None, bytes | None]], *, producer_id: int,
                        producer_epoch: int, base_sequence: int, transactional: bool) -> bytes:
    now = int(time.time() * 1000)
    body = bytearray()
    for delta, (key, value) in enumerate(records):
        rec = i8(0) + varint(0) + varint(delta)
        rec += varint(-1) if key is None else varint(len(key)) + key
        rec += varint(-1) if value is None else varint(len(value)) + value
        rec += varint(0)  # headers
        body += varint(len(rec)) + rec
    attributes = ATTR_TRANSACTIONAL if transactional else 0
    after_crc = (i16(attributes) + i32(len(records) - 1) + i64(now) + i64(now) + i64(producer_id)
                 + i16(producer_epoch) + i32(base_sequence) + i32(len(records)) + bytes(body))
    crc = struct.pack(">I", crc32c(after_crc))
    after_length = i32(-1) + i8(2) + crc + after_crc  # partition leader epoch, magic, crc
    return i64(0) + i32(len(after_length)) + after_length


def decode_record_batches(data: bytes | None) -> list[RecordBatch]:
    batches: list[RecordBatch] = []
    if not data:
        return batches
    pos = 0
    while pos + 12 <= len(data):
        base_offset, length = struct.unpack_from(">qi", data, pos)
        end = pos + 12 + length
        if end > len(data):
            break  # a partial batch at the end of a fetch response
        r = Reader(data, pos + 12)
        r.i32()  # partition leader epoch
        magic = r.i8()
        if magic != 2:
            raise ValueError(f"unsupported record batch magic {magic}")
        r.u32()  # crc
        attributes = r.i16()
        last_delta = r.i32()
        r.i64()  # base timestamp
        r.i64()  # max timestamp
        producer_id = r.i64()
        producer_epoch = r.i16()
        base_sequence = r.i32()
        count = r.i32()
        batch = RecordBatch(base_offset, base_offset + last_delta, producer_id, producer_epoch,
                            base_sequence, attributes)
        if attributes & 0x07:
            raise ValueError("compressed record batches are not supported by this reader")
        for _ in range(count):
            r.varint()  # record length
            r.i8()
            ts_delta = r.varint()
            off_delta = r.varint()
            klen = r.varint()
            key = None if klen < 0 else r.raw(klen)
            vlen = r.varint()
            value = None if vlen < 0 else r.raw(vlen)
            for _ in range(r.varint()):
                r.raw(r.varint())
                hv = r.varint()
                if hv >= 0:
                    r.raw(hv)
            batch.records.append(Record(base_offset + off_delta, key, value, ts_delta))
        batches.append(batch)
        pos = end
    return batches


# ── connection ─────────────────────────────────────────────────────────────────────


def _read_exactly(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionClosed("connection closed by broker")
        buf += chunk
    return bytes(buf)


class BrokerConnection:
    """One TCP connection to one broker; one request in flight at a time."""

    def __init__(self, host: str, port: int, client_id: str, connect_timeout_s: float = 5.0):
        self.host, self.port, self.client_id = host, port, client_id
        self._sock: socket.socket | None = socket.create_connection((host, port), timeout=connect_timeout_s)
        self._correlation = 0

    @property
    def closed(self) -> bool:
        return self._sock is None

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def request(self, api_key: int, body: bytes, timeout_s: float, version: int | None = None) -> Reader:
        """Send one request and wait up to ``timeout_s`` for its response.

        On a timeout the connection is closed and ``RequestTimedOut`` is raised: the bytes were
        written, so the broker may still act on the request.
        """
        if self._sock is None:
            raise ConnectionClosed("connection already closed")
        version = VERSIONS[api_key] if version is None else version
        self._correlation += 1
        correlation = self._correlation
        header = i16(api_key) + i16(version) + i32(correlation) + string(self.client_id)
        flexible = api_key in FLEXIBLE
        if flexible:
            header += TAGS_NONE
        frame = header + body
        try:
            self._sock.settimeout(max(timeout_s, 0.001))
            self._sock.sendall(i32(len(frame)) + frame)
            size = struct.unpack(">i", _read_exactly(self._sock, 4))[0]
            data = _read_exactly(self._sock, size)
        except socket.timeout as exc:
            self.close()
            raise RequestTimedOut(f"no response to api {api_key} within {timeout_s:.1f}s") from exc
        except OSError as exc:
            self.close()
            if isinstance(exc, ConnectionClosed):
                raise
            raise ConnectionClosed(str(exc)) from exc
        r = Reader(data)
        if r.i32() != correlation:
            self.close()
            raise ConnectionClosed("correlation id mismatch")
        if flexible:
            r.tags()
        return r


# ── request bodies and response decoders ───────────────────────────────────────────


def api_versions_request() -> bytes:
    return b""


def api_versions_response(r: Reader) -> tuple[int, dict[int, tuple[int, int]]]:
    error = r.i16()
    ranges = dict(r.array(lambda x: (x.i16(), (x.i16(), x.i16()))))
    return error, ranges


@dataclass
class BrokerInfo:
    node_id: int
    host: str
    port: int


@dataclass
class PartitionInfo:
    error_code: int
    partition: int
    leader: int


@dataclass
class TopicInfo:
    error_code: int
    name: str
    partitions: list[PartitionInfo]


def metadata_request(topics: list[str] | None) -> bytes:
    if topics is None:
        return i32(-1)
    return array([string(t) for t in topics])


def _metadata_broker(x: Reader) -> BrokerInfo:
    node_id = x.i32()
    host = x.string()
    port = x.i32()
    x.string()  # rack
    return BrokerInfo(node_id, host, port)


def _metadata_partition(x: Reader) -> PartitionInfo:
    error = x.i16()
    index = x.i32()
    leader = x.i32()
    x.array(lambda y: y.i32())  # replicas
    x.array(lambda y: y.i32())  # isr
    return PartitionInfo(error, index, leader)


def _metadata_topic(x: Reader) -> TopicInfo:
    error = x.i16()
    name = x.string()
    x.boolean()  # is_internal
    return TopicInfo(error, name, x.array(_metadata_partition))


def metadata_response(r: Reader) -> tuple[dict[int, BrokerInfo], dict[str, TopicInfo]]:
    brokers = {b.node_id: b for b in r.array(_metadata_broker)}
    r.i32()  # controller id
    topics = {t.name: t for t in r.array(_metadata_topic)}
    return brokers, topics


def find_coordinator_request(key: str, key_type: int = COORDINATOR_TRANSACTION) -> bytes:
    return string(key) + i8(key_type)


def find_coordinator_response(r: Reader) -> tuple[int, BrokerInfo | None]:
    r.i32()  # throttle
    error = r.i16()
    r.string()  # error message
    node_id = r.i32()
    host = r.string()
    port = r.i32()
    return error, (BrokerInfo(node_id, host, port) if error == 0 else None)


def init_producer_id_request(transactional_id: str | None, transaction_timeout_ms: int,
                             producer_id: int = NO_PRODUCER_ID, producer_epoch: int = NO_PRODUCER_EPOCH) -> bytes:
    """InitProducerId v3 (KIP-360 fields).

    With ``producer_id``/``producer_epoch`` left at -1 the coordinator treats the caller as a new
    instance of the transactional id: it aborts whatever transaction is open for that id and hands
    out a newer epoch, which fences every instance holding an older one. With the caller's current
    producer id and epoch it moves to a newer epoch only if that pair is still the current one, and
    answers INVALID_PRODUCER_EPOCH (or PRODUCER_FENCED) otherwise. Against a transaction that is
    still open, the coordinator first aborts it and answers CONCURRENT_TRANSACTIONS; retry the same
    request. That abort is decided on the producer id alone, before the epoch is compared, so it
    happens whoever opened the transaction: a caller holding an older epoch of the current producer
    id still aborts the transaction the current owner has open, and only its retry is refused.
    """
    return (compact_string(transactional_id) + i32(transaction_timeout_ms) + i64(producer_id)
            + i16(producer_epoch) + TAGS_NONE)


def init_producer_id_response(r: Reader) -> tuple[int, int, int]:
    r.i32()  # throttle
    error = r.i16()
    producer_id = r.i64()
    producer_epoch = r.i16()
    return error, producer_id, producer_epoch


def add_partitions_request(transactional_id: str, producer_id: int, producer_epoch: int,
                           partitions: dict[str, list[int]]) -> bytes:
    topics = [string(t) + array([i32(p) for p in ps]) for t, ps in partitions.items()]
    return string(transactional_id) + i64(producer_id) + i16(producer_epoch) + array(topics)


def add_partitions_response(r: Reader) -> dict[tuple[str, int], int]:
    r.i32()  # throttle
    out: dict[tuple[str, int], int] = {}
    for _ in range(r.i32()):
        topic = r.string()
        for _ in range(r.i32()):
            partition = r.i32()
            out[(topic, partition)] = r.i16()
    return out


def produce_request(transactional_id: str | None, topic: str, partition: int, records: bytes,
                    acks: int = -1, timeout_ms: int = 30000) -> bytes:
    data = string(topic) + array([i32(partition) + nbytes(records)])
    return string(transactional_id) + i16(acks) + i32(timeout_ms) + array([data])


def produce_response(r: Reader) -> dict[tuple[str, int], tuple[int, int]]:
    out: dict[tuple[str, int], tuple[int, int]] = {}
    for _ in range(r.i32()):
        topic = r.string()
        for _ in range(r.i32()):
            partition = r.i32()
            error = r.i16()
            base_offset = r.i64()
            r.i64()  # log append time
            r.i64()  # log start offset
            out[(topic, partition)] = (error, base_offset)
    return out


def end_txn_request(transactional_id: str, producer_id: int, producer_epoch: int, commit: bool) -> bytes:
    return string(transactional_id) + i64(producer_id) + i16(producer_epoch) + i8(1 if commit else 0)


def end_txn_response(r: Reader) -> int:
    r.i32()  # throttle
    return r.i16()


def list_offsets_request(topic: str, partition: int, timestamp: int, isolation_level: int) -> bytes:
    return i32(-1) + i8(isolation_level) + array([string(topic) + array([i32(partition) + i64(timestamp)])])


def list_offsets_response(r: Reader) -> tuple[int, int]:
    r.i32()  # throttle
    result = (-1, -1)
    for _ in range(r.i32()):
        r.string()
        for _ in range(r.i32()):
            r.i32()
            error = r.i16()
            r.i64()  # timestamp
            result = (error, r.i64())
    return result


def fetch_request(topic: str, partition: int, offset: int, isolation_level: int,
                  max_wait_ms: int = 100, max_bytes: int = 4 * 1024 * 1024) -> bytes:
    part = i32(partition) + i64(offset) + i32(max_bytes)
    return (i32(-1) + i32(max_wait_ms) + i32(1) + i32(max_bytes) + i8(isolation_level)
            + array([string(topic) + array([part])]))


@dataclass
class FetchedPartition:
    error_code: int
    high_watermark: int
    last_stable_offset: int
    aborted: list[tuple[int, int]]  # (producer id, first offset)
    batches: list[RecordBatch]


def fetch_response(r: Reader) -> FetchedPartition:
    r.i32()  # throttle
    result = FetchedPartition(-1, -1, -1, [], [])
    for _ in range(r.i32()):
        r.string()
        for _ in range(r.i32()):
            r.i32()  # partition
            error = r.i16()
            hwm = r.i64()
            lso = r.i64()
            aborted = r.array(lambda x: (x.i64(), x.i64()))
            records = r.nbytes()
            result = FetchedPartition(error, hwm, lso, aborted, decode_record_batches(records))
    return result
