"""Give a lease's slot back exactly once.

Two paths return slots: the orchestrator when a run leaves its process (terminal state,
reschedule) and the repossessor when a lease expires. They write to two stores with no common
transaction (the counter in Postgres, the lease in Valkey), and either process can die between the
two writes. The lease_release row is the arbiter: it is inserted in the same Postgres transaction
as the decrement, only the transaction that inserts it decrements, and the Valkey record is
deleted only after that transaction committed. A record still in Valkey for a lease listed in
lease_release is dead and is cleaned up by whoever meets it next.
"""

from __future__ import annotations

import logging
import uuid

import psycopg

from . import slots
from .leases import Lease

logger = logging.getLogger("orchestrator.release")


def is_released(conn: psycopg.Connection, lease_id: uuid.UUID | str) -> bool:
    return conn.execute("SELECT 1 FROM lease_release WHERE lease_id = %s", (lease_id,)).fetchone() is not None


def consume_lease(
    conn: psycopg.Connection,
    lease_id: uuid.UUID | str,
    lease: Lease | None,
    fallback_limit_ids: list[uuid.UUID] | None,
    released_by: str,
) -> bool:
    """Inside the caller's transaction. Returns True if this call gave the slot back.

    `lease` is the Valkey record read by the caller (None if it was not found). A missing record
    that nobody has released was lost from storage; its slot is given back on `fallback_limit_ids`.
    """
    row = conn.execute(
        "INSERT INTO lease_release (lease_id, released_by) VALUES (%s, %s) "
        "ON CONFLICT (lease_id) DO NOTHING RETURNING lease_id",
        (lease_id, released_by),
    ).fetchone()
    if row is None:
        logger.info("Lease %s was already released; nothing to give back", lease_id)
        return False
    if lease is not None:
        limit_ids, count = lease.resource_ids, lease.slots
    elif fallback_limit_ids:
        logger.warning("Lease %s not found and never released; releasing its slot", lease_id)
        limit_ids, count = fallback_limit_ids, 1
    else:
        return True
    if not slots.bulk_decrement_active_slots(conn, limit_ids, count):
        raise RuntimeError(f"Failed to release concurrency slots for lease {lease_id}")
    return True
