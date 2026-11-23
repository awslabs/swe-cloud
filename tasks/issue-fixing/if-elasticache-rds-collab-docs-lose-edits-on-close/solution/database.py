"""Postgres persistence: load a document from its ``documents`` row, store it back.

The row holds the full document: ``state`` is the complete Yjs update of the document
(``encode_state_as_update``), so loading a document is applying that one update to an empty doc.
See ``schema.sql`` for the table contract.
"""

from __future__ import annotations

import logging
import time

import psycopg

log = logging.getLogger("docserver.database")

FETCH = "SELECT state FROM documents WHERE name = %s"

READ = "SELECT state, version FROM documents WHERE name = %s"

CREATE = "INSERT INTO documents (name, state, version) VALUES (%s, %s, 1) ON CONFLICT (name) DO NOTHING"

# Conditional on the version the merge was based on: a row another writer committed in the
# meantime matches no row, and the store re-reads and merges again.
UPDATE = "UPDATE documents SET state = %s, version = version + 1 WHERE name = %s AND version = %s"


class DatabaseExtension:
    priority = 100

    def __init__(self, dsn: str, instance_id: str):
        self.dsn = dsn
        self.instance_id = instance_id

    def connect(self) -> psycopg.Connection:
        return psycopg.connect(self.dsn, connect_timeout=5, application_name=f"docserver-{self.instance_id}")

    def on_load_document(self, payload) -> None:
        """Get the stored state from the database and apply it to the fresh document."""
        with self.connect() as conn:
            row = conn.execute(FETCH, (payload.document_name,)).fetchone()
        if row is not None:
            payload.document.apply_update(bytes(row[0]))

    def on_store_document(self, payload) -> None:
        """Merge the stored row into the document and write the result back, conditionally.

        Other instances write the same row and may hold edits this instance never received, so the
        row is merged in (Yjs updates are commutative and idempotent) and the write only lands on
        the version that was read. A conflict or a lost connection retries; this returns only once
        the document's state is committed.
        """
        name, document = payload.document_name, payload.document
        attempt = 0
        while True:
            try:
                with self.connect() as conn:
                    row = conn.execute(READ, (name,)).fetchone()
                    if row is None:
                        written = conn.execute(CREATE, (name, document.encode_state_as_update())).rowcount
                    else:
                        document.apply_update(bytes(row[0]))
                        written = conn.execute(UPDATE, (document.encode_state_as_update(), name, row[1])).rowcount
                if written == 1:
                    log.info("stored %s (attempt %d)", name, attempt + 1)
                    return
                log.info("store of %s lost a race with another writer; merging again", name)
            except (psycopg.OperationalError, psycopg.InterfaceError) as error:
                log.warning("store of %s failed (%s); retrying", name, error)
            attempt += 1
            time.sleep(min(0.02 * 2**attempt, 0.5))
