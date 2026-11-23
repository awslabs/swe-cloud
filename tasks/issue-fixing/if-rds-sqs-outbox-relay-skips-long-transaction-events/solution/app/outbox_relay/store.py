"""Postgres access for the relay: the outbox read and the checkpoint row."""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass

import psycopg

log = logging.getLogger("relay.store")


@dataclass(frozen=True)
class OutboxRow:
    position: int
    message_id: str
    message_type: str
    data: str  # the jsonb column rendered by Postgres


def connect(retry_for: float = 10.0) -> psycopg.Connection:
    """Connect with the libpq environment (PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD)."""
    deadline = time.monotonic() + retry_for
    while True:
        try:
            return psycopg.connect(autocommit=True, application_name="outbox-relay", connect_timeout=5)
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)


class OutboxSource:
    def __init__(self, conn: psycopg.Connection):
        self.conn = conn

    def read_after(self, position: int, limit: int) -> tuple[int, list[OutboxRow]]:
        """Rows above `position` and the xmin of the snapshot they were read in (one statement)."""
        cur = self.conn.execute(
            "with s as (select pg_snapshot_xmin(pg_current_snapshot())::text::bigint as xmin) "
            "select s.xmin, o.position, o.message_id, o.message_type, o.data from s left join lateral "
            "(select position, message_id, message_type, data::text as data from outbox "
            " where position > %s order by position limit %s) o on true order by o.position",
            (position, limit),
        )
        result = cur.fetchall()
        return result[0][0], [OutboxRow(*r[1:]) for r in result if r[1] is not None]

    def take_xid(self) -> int:
        """A transaction id assigned now (autocommit: the transaction ends with the statement)."""
        return int(self.conn.execute("select pg_current_xact_id()::text").fetchone()[0])


class CheckpointStore:
    def __init__(self, conn: psycopg.Connection):
        self.conn = conn

    def read(self) -> tuple[int, dict]:
        position, meta = self.conn.execute("select position, meta from relay_checkpoint where id = 1").fetchone()
        return position, meta

    def advance(self, position: int, meta: dict | None = None) -> bool:
        """Move the checkpoint forward to `position`. Never moves it backwards."""
        meta = {"pid": os.getpid(), **(meta or {})}
        cur = self.conn.execute(
            "update relay_checkpoint set position = %s, meta = %s::jsonb, updated_at = now() "
            "where id = 1 and position <= %s",
            (position, json.dumps(meta), position),
        )
        if cur.rowcount != 1:
            log.warning("checkpoint not advanced to %s: stored position is ahead", position)
            return False
        return True
