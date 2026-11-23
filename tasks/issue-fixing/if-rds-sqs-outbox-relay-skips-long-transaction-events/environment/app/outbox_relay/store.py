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

    def read_after(self, position: int, limit: int) -> list[OutboxRow]:
        cur = self.conn.execute(
            "select position, message_id, message_type, data::text from outbox "
            "where position > %s order by position limit %s",
            (position, limit),
        )
        return [OutboxRow(*r) for r in cur.fetchall()]


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
