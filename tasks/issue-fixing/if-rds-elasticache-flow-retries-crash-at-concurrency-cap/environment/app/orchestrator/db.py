"""Postgres access: connections and schema bootstrap."""

from __future__ import annotations

import time
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

SCHEMA = Path(__file__).with_name("schema.sql")


def connect(dsn: str, attempts: int = 20) -> psycopg.Connection:
    last: Exception | None = None
    for _ in range(attempts):
        try:
            return psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5, autocommit=True)
        except psycopg.OperationalError as exc:
            last = exc
            time.sleep(0.5)
    raise RuntimeError(f"could not connect to Postgres: {last}")


def ensure_schema(conn: psycopg.Connection) -> None:
    with conn.transaction():
        # Serialise concurrent bootstraps (orchestrator and repossessor start together).
        conn.execute("SELECT pg_advisory_xact_lock(4200)")
        conn.execute(SCHEMA.read_text())
