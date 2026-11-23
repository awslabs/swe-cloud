"""Postgres persistence: load a document from its ``documents`` row, store it back.

The row holds the full document: ``state`` is the complete Yjs update of the document
(``encode_state_as_update``), so loading a document is applying that one update to an empty doc.
See ``schema.sql`` for the table contract.
"""

from __future__ import annotations

import logging

import psycopg

log = logging.getLogger("docserver.database")

FETCH = "SELECT state FROM documents WHERE name = %s"

STORE = """
INSERT INTO documents (name, state, version) VALUES (%s, %s, 1)
ON CONFLICT (name) DO UPDATE SET state = EXCLUDED.state, version = documents.version + 1
"""


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
        """Store the document's full state."""
        state = payload.document.encode_state_as_update()
        with self.connect() as conn:
            conn.execute(STORE, (payload.document_name, state))
        log.info("stored %s (%d bytes)", payload.document_name, len(state))
