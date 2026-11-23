"""An in-memory collaborative document: a Yjs doc whose ``edits`` map holds one entry per edit."""

from __future__ import annotations

import threading

import pycrdt


class Document:
    def __init__(self, name: str):
        self.name = name
        self.ydoc = pycrdt.Doc()
        self.edits = self.ydoc.get("edits", type=pycrdt.Map)
        self.connections = 0
        self.pending_stores = 0
        self.save_mutex = threading.Lock()
        self._lock = threading.Lock()  # guards ydoc against concurrent HTTP / fan-out threads

    def apply_update(self, update: bytes) -> None:
        with self._lock:
            self.ydoc.apply_update(update)

    def edit(self, edit_id: str) -> bytes:
        """Apply a local edit; return the incremental update it produced."""
        with self._lock:
            before = self.ydoc.get_state()
            self.edits[edit_id] = edit_id
            return self.ydoc.get_update(before)

    def encode_state_as_update(self) -> bytes:
        with self._lock:
            return self.ydoc.get_update()

    def edit_ids(self) -> list[str]:
        with self._lock:
            return sorted(self.edits.keys())
