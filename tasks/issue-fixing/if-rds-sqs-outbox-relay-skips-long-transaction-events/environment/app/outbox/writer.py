"""Producer side of the outbox. Every service that emits ledger events goes through this module.

Usage::

    conn = psycopg.connect(...)              # role app_writer, autocommit off
    writer = OutboxWriter(conn)
    with writer.transaction() as tx:         # BEGIN; SELECT pg_current_xact_id()
        ...business writes on conn...
        tx.append("payment-81723", "PaymentCaptured", {"amount": 1200})
    # COMMIT on clean exit, ROLLBACK on exception

The transaction id is taken as the first statement of every transaction, before any insert, so
that log lines and traces can name the transaction that produced a message. Positions are only
ever drawn by the INSERT's column default; nothing calls nextval() directly.

`append` is idempotent per message_id: a retried producer that re-sends an id already in the
outbox is dropped by ON CONFLICT DO NOTHING and gets None back.
"""
from __future__ import annotations

import contextlib
import json
import logging

log = logging.getLogger("outbox.writer")

_INSERT = (
    "insert into outbox (message_id, message_type, data) values (%s, %s, %s::jsonb) "
    "on conflict (message_id) do nothing returning position"
)


class OutboxTransaction:
    def __init__(self, conn, xid: int):
        self.conn = conn
        self.xid = xid

    def append(self, message_id: str, message_type: str, data: dict) -> int | None:
        row = self.conn.execute(_INSERT, (message_id, message_type, json.dumps(data))).fetchone()
        if row is None:
            log.info("xid=%s message %s already in outbox, dropped", self.xid, message_id)
            return None
        return row[0]


class OutboxWriter:
    def __init__(self, conn):
        if conn.autocommit:
            raise ValueError("OutboxWriter needs a connection with autocommit off")
        self.conn = conn

    @contextlib.contextmanager
    def transaction(self):
        with self.conn.transaction():
            xid = int(self.conn.execute("select pg_current_xact_id()::text").fetchone()[0])
            log.debug("xid=%s begin", xid)
            yield OutboxTransaction(self.conn, xid)
