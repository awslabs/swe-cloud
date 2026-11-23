"""The indexer's own bookkeeping in Postgres (schema reindex_aux, owned by the catalog_indexer role).

A run is ``backfilling`` from just after its index exists until the alias has moved to it: while a run
is backfilling, the hook sends every live change to the run's index as well, and records deletes in
``pending_deletes`` so the reindex can re-apply them once no backfill page can bring a row back.

A run stays backfilling when its process dies; ``resume_after`` is the last product id whose page is
in the run's index, so a rerun continues the same run (same index, same recorded deletes) from there.

A drained job is paused, not killed: its session stays open and keeps the reindex lock. The running
reindex heartbeats ``reindex_owners`` from a thread of its own; a rerun that finds the lock held and the
heartbeat stopped terminates the holder's session, which releases the lock.
"""

from __future__ import annotations

import queue
import threading
import time
from contextlib import contextmanager

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
);
ALTER TABLE reindex_aux.reindex_runs ADD COLUMN IF NOT EXISTS resume_after bigint NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS reindex_aux.reindex_owners (
    alias        text        PRIMARY KEY,
    backend_pid  int         NOT NULL,
    heartbeat_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS reindex_aux.pending_deletes (
    run_id     bigint      NOT NULL,
    product_id bigint      NOT NULL,
    logged_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, product_id)
);
"""


def connect() -> psycopg.Connection:
    return psycopg.connect(config.indexer_dsn(), autocommit=True)


def ensure_schema(conn: psycopg.Connection) -> None:
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('reindex_aux.schema'))")
        for statement in filter(str.strip, SCHEMA.split(";")):
            conn.execute(statement)


def start_run(conn: psycopg.Connection, alias: str, index_name: str, schema_version: int) -> int:
    row = conn.execute(
        "INSERT INTO reindex_aux.reindex_runs (alias, index_name, schema_version, status)"
        " VALUES (%s, %s, %s, 'backfilling') RETURNING id",
        (alias, index_name, schema_version),
    ).fetchone()
    return row[0]


OWNER_STALE_S = 5.0


def lock_reindex(conn: psycopg.Connection, alias: str, wait_s: float = 30.0) -> bool:
    """One reindex process per alias. The lock is a session lock, and a paused job's session keeps it:
    when its heartbeat has stopped, end that session (the role may terminate its own backends)."""
    deadline = time.monotonic() + wait_s
    while True:
        if conn.execute("SELECT pg_try_advisory_lock(hashtext('reindex_aux.reindex:' || %s))", (alias,)).fetchone()[0]:
            conn.execute("INSERT INTO reindex_aux.reindex_owners (alias, backend_pid) VALUES (%s, pg_backend_pid())"
                         " ON CONFLICT (alias) DO UPDATE SET backend_pid = EXCLUDED.backend_pid, heartbeat_at = now()", (alias,))
            return True
        stale = conn.execute(
            "SELECT backend_pid FROM reindex_aux.reindex_owners"
            " WHERE alias = %s AND heartbeat_at < now() - make_interval(secs => %s)", (alias, OWNER_STALE_S)).fetchone()
        if stale is not None:
            conn.execute("SELECT pg_terminate_backend(pid, 5000) FROM pg_stat_activity WHERE pid = %s AND usename = current_user",
                         (stale[0],))
        elif time.monotonic() > deadline:
            return False
        time.sleep(1.0)


def heartbeat(alias: str, backend_pid: int) -> None:
    """Keep the lock holder's heartbeat fresh while this process runs, including while it waits on the export API."""
    def beat() -> None:
        with connect() as conn:
            while True:
                conn.execute("UPDATE reindex_aux.reindex_owners SET heartbeat_at = now() WHERE alias = %s AND backend_pid = %s",
                             (alias, backend_pid))
                time.sleep(1.0)
    threading.Thread(target=beat, daemon=True).start()


def resumable_run(conn: psycopg.Connection, alias: str, schema_version: int) -> tuple[int, str, int] | None:
    return conn.execute(
        "SELECT id, index_name, resume_after FROM reindex_aux.reindex_runs"
        " WHERE alias = %s AND schema_version = %s AND status = 'backfilling' ORDER BY id DESC LIMIT 1",
        (alias, schema_version),
    ).fetchone()


def save_cursor(conn: psycopg.Connection, run_id: int, after_id: int) -> None:
    conn.execute("UPDATE reindex_aux.reindex_runs SET resume_after = %s WHERE id = %s", (after_id, run_id))


def finish_run(conn: psycopg.Connection, run_id: int, status: str) -> None:
    conn.execute(
        "UPDATE reindex_aux.reindex_runs SET status = %s, finished_at = now() WHERE id = %s",
        (status, run_id),
    )


def active_run(conn: psycopg.Connection, alias: str) -> tuple[int, str] | None:
    return conn.execute(
        "SELECT id, index_name FROM reindex_aux.reindex_runs WHERE alias = %s AND status = 'backfilling'"
        " ORDER BY id DESC LIMIT 1",
        (alias,),
    ).fetchone()


def log_delete(conn: psycopg.Connection, run_id: int, product_id: int) -> None:
    conn.execute(
        "INSERT INTO reindex_aux.pending_deletes (run_id, product_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (run_id, product_id),
    )


def pending_deletes(conn: psycopg.Connection, run_id: int) -> list[int]:
    return [r[0] for r in conn.execute(
        "SELECT product_id FROM reindex_aux.pending_deletes WHERE run_id = %s ORDER BY product_id", (run_id,)).fetchall()]


class Pool:
    """A few reusable connections for the hook server's request threads."""

    def __init__(self) -> None:
        self.idle: queue.LifoQueue = queue.LifoQueue()

    @contextmanager
    def connection(self):
        try:
            conn = self.idle.get_nowait()
        except queue.Empty:
            conn = connect()
        broken = False
        try:
            yield conn
        except psycopg.Error:
            broken = True
            raise
        finally:
            if broken or conn.closed:
                conn.close()
            else:
                self.idle.put(conn)
