"""The document registry: open, edit, close, store and unload.

A document is loaded on its first ``open`` and stays in memory while it has connections. When
the last connection closes, the instance stores the document (``on_store_document`` then
``after_store_document`` on every extension, under the document's save mutex) and then unloads it.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from .document import Document
from .hooks import HookChain, SkipFurtherHooks

log = logging.getLogger("docserver.server")


@dataclass
class LoadPayload:
    document_name: str
    document: Document
    instance_id: str


@dataclass
class ChangePayload:
    document_name: str
    document: Document
    update: bytes
    origin: str  # "local" for this instance's connections, "redis" for fan-out


@dataclass
class StorePayload:
    document_name: str
    document: Document
    instance_id: str


class NotOpen(Exception):
    pass


class DocServer:
    def __init__(self, instance_id: str, extensions):
        self.instance_id = instance_id
        self.hooks = HookChain(extensions)
        self.documents: dict[str, Document] = {}
        self._names: dict[str, threading.Lock] = {}
        self._registry = threading.Lock()
        for extension in self.hooks.extensions:
            configure = getattr(extension, "on_configure", None)
            if configure:
                configure(self)

    def _name_lock(self, name: str) -> threading.Lock:
        with self._registry:
            return self._names.setdefault(name, threading.Lock())

    # ── connections ────────────────────────────────────────────────────────────

    def open(self, name: str) -> Document:
        with self._name_lock(name):
            document = self.documents.get(name)
            if document is None:
                document = Document(name)
                self.hooks.run("on_load_document", LoadPayload(name, document, self.instance_id))
                self.documents[name] = document
                log.info("loaded %s", name)
            document.connections += 1
            return document

    def edit(self, name: str, edit_id: str) -> None:
        document = self.documents.get(name)
        if document is None or document.connections == 0:
            raise NotOpen(name)
        update = document.edit(edit_id)
        self.hooks.run("on_change", ChangePayload(name, document, update, "local"))

    def apply_remote_update(self, name: str, update: bytes) -> None:
        document = self.documents.get(name)
        if document is not None:
            document.apply_update(update)

    def close(self, name: str) -> None:
        with self._name_lock(name):
            document = self.documents.get(name)
            if document is None or document.connections == 0:
                raise NotOpen(name)
            document.connections -= 1
            if document.connections > 0:
                return
            document.pending_stores += 1
        threading.Thread(target=self.store_document_hooks, args=(document,), name=f"store-{name}", daemon=True).start()

    def status(self, name: str) -> dict:
        document = self.documents.get(name)
        if document is None:
            return {"loaded": False, "connections": 0}
        return {"loaded": True, "connections": document.connections, "edits": document.edit_ids()}

    # ── store and unload ───────────────────────────────────────────────────────

    def store_document_hooks(self, document: Document) -> None:
        payload = StorePayload(document.name, document, self.instance_id)
        try:
            try:
                with document.save_mutex:
                    self.hooks.run("on_store_document", payload)
                    self.hooks.run("after_store_document", payload)
            finally:
                with self._name_lock(document.name):
                    document.pending_stores -= 1
        except SkipFurtherHooks as skip:
            # Another extension handled this — proceed to unload.
            log.info("store of %s skipped: %s", document.name, skip)
            self.unload_if_idle(document)
            return
        except Exception:
            log.exception("error while storing %s. Document stays in memory to avoid data loss", document.name)
            return
        self.unload_if_idle(document)

    def should_unload(self, document: Document) -> bool:
        return document.connections == 0 and document.pending_stores == 0 and not document.save_mutex.locked()

    def unload_if_idle(self, document: Document) -> None:
        with self._name_lock(document.name):
            if self.documents.get(document.name) is document and self.should_unload(document):
                del self.documents[document.name]
                log.info("unloaded %s", document.name)
