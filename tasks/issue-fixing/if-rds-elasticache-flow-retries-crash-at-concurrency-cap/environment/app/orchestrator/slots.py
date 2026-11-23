"""Deployment concurrency slot counters (concurrency_limit_v2.active_slots)."""

from __future__ import annotations

import uuid

import psycopg


def bulk_increment_active_slots(conn: psycopg.Connection, limit_ids: list[uuid.UUID], slots: int) -> bool:
    """Take `slots` on every limit, or on none: the UPDATE only matches rows with room left."""
    cur = conn.execute(
        """
        UPDATE concurrency_limit_v2
           SET active_slots = active_slots + %(slots)s, updated = clock_timestamp()
         WHERE id = ANY(%(ids)s) AND active AND active_slots + %(slots)s <= "limit"
        """,
        {"ids": list(limit_ids), "slots": slots},
    )
    return cur.rowcount == len(limit_ids)


def bulk_decrement_active_slots(conn: psycopg.Connection, limit_ids: list[uuid.UUID], slots: int) -> bool:
    """Give back `slots` on every limit, clamped at zero."""
    cur = conn.execute(
        """
        UPDATE concurrency_limit_v2
           SET active_slots = GREATEST(active_slots - %(slots)s, 0), updated = clock_timestamp()
         WHERE id = ANY(%(ids)s) AND active
        """,
        {"ids": list(limit_ids), "slots": slots},
    )
    return cur.rowcount == len(limit_ids)
