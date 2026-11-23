"""The indexer's own bookkeeping in Postgres (schema reindex_aux, owned by the catalog_indexer role)."""

from __future__ import annotations

import psycopg

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS reindex_aux.reindex_runs (
    id             bigserial PRIMARY KEY,
    alias          text        NOT NULL,
    index_name     text        NOT NULL,
    schema_version int         NOT NULL,
    status         text        NOT NULL,
    started_at     timestamptz NOT NULL DEFAULT now(),
    finished_at    timestamptz
)
"""


def connect() -> psycopg.Connection:
    return psycopg.connect(config.indexer_dsn(), autocommit=True)


def ensure_schema(conn: psycopg.Connection) -> None:
    conn.execute(SCHEMA)


def start_run(conn: psycopg.Connection, alias: str, index_name: str, schema_version: int) -> int:
    row = conn.execute(
        "INSERT INTO reindex_aux.reindex_runs (alias, index_name, schema_version, status)"
        " VALUES (%s, %s, %s, 'running') RETURNING id",
        (alias, index_name, schema_version),
    ).fetchone()
    return row[0]


def finish_run(conn: psycopg.Connection, run_id: int, status: str) -> None:
    conn.execute(
        "UPDATE reindex_aux.reindex_runs SET status = %s, finished_at = now() WHERE id = %s",
        (status, run_id),
    )
