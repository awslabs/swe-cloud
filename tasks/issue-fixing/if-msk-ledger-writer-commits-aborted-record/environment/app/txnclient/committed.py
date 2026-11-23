"""Read a partition the way a read_committed consumer sees it: committed records only, up to the LSO."""

from __future__ import annotations

from . import errors
from . import protocol as p
from .producer import parse_bootstrap


def _leader(bootstrap: str, topic: str, partition: int, client_id: str) -> p.BrokerConnection:
    last: Exception | None = None
    for host, port in parse_bootstrap(bootstrap):
        try:
            conn = p.BrokerConnection(host, port, client_id)
        except OSError as exc:
            last = exc
            continue
        try:
            brokers, topics = p.metadata_response(conn.request(p.METADATA, p.metadata_request([topic]), 10))
        finally:
            conn.close()
        info = topics.get(topic)
        if info is None or info.error_code:
            raise RuntimeError(f"topic {topic}: {errors.name(info.error_code if info else -1)}")
        for part in info.partitions:
            if part.partition == partition and part.leader in brokers:
                b = brokers[part.leader]
                return p.BrokerConnection(b.host, b.port, client_id)
        raise RuntimeError(f"no leader for {topic}-{partition}")
    raise RuntimeError(f"no bootstrap broker reachable: {last}")


def read_committed(bootstrap: str, topic: str, partition: int = 0, client_id: str = "ledger-reader") -> list[p.Record]:
    conn = _leader(bootstrap, topic, partition, client_id)
    try:
        error, lso = p.list_offsets_response(conn.request(
            p.LIST_OFFSETS, p.list_offsets_request(topic, partition, p.LATEST, p.READ_COMMITTED), 10))
        if error:
            raise RuntimeError(f"ListOffsets {topic}-{partition}: {errors.name(error)}")
        out: list[p.Record] = []
        aborted_pending: list[tuple[int, int]] = []
        seen_aborted: set[tuple[int, int]] = set()
        aborting: set[int] = set()
        offset = 0
        while offset < lso:
            fetched = p.fetch_response(conn.request(
                p.FETCH, p.fetch_request(topic, partition, offset, p.READ_COMMITTED), 10))
            if fetched.error_code:
                raise RuntimeError(f"Fetch {topic}-{partition}@{offset}: {errors.name(fetched.error_code)}")
            for entry in fetched.aborted:
                if entry not in seen_aborted:
                    seen_aborted.add(entry)
                    aborted_pending.append(entry)
            aborted_pending.sort(key=lambda e: e[1])
            if not fetched.batches:
                break
            for batch in fetched.batches:
                if batch.last_offset < offset:
                    continue
                while aborted_pending and aborted_pending[0][1] <= batch.last_offset:
                    aborting.add(aborted_pending.pop(0)[0])
                if batch.control:
                    if batch.control_type() == 0:
                        aborting.discard(batch.producer_id)
                elif not (batch.transactional and batch.producer_id in aborting):
                    out.extend(r for r in batch.records if offset <= r.offset < lso)
                offset = batch.last_offset + 1
        return out
    finally:
        conn.close()
