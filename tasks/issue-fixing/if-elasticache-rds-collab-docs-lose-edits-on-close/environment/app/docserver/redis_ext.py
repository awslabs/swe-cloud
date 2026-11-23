"""Valkey extension: cross-instance fan-out of document updates, and the store lock.

Every instance subscribes to ``{prefix}:{document}`` for the documents it has loaded and publishes
its local updates there, so instances that hold the same document converge in memory. Fan-out is
Valkey pub/sub: at most once, nothing is replayed to a subscriber that was disconnected when a
message was published. Updates applied from fan-out are never stored by the receiving instance;
each instance stores its own document when its own last connection closes.

Before a document is stored, the extension takes ``{prefix}:{document}:lock`` (Redlock acquire,
``PX lock_timeout_ms``) so that only one instance stores a document at a time, and releases it
(compare-and-delete) after the store.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time

import redis

from .hooks import SkipFurtherHooks
from .redlock import QUORUM_MESSAGE, ExecutionError, Redlock

log = logging.getLogger("docserver.redis")


class RedisExtension:
    priority = 1000

    def __init__(self, url: str, prefix: str, instance_id: str, lock_timeout_ms: int = 1000):
        self.prefix = prefix
        self.instance_id = instance_id
        # The maximum time for the lock in ms (in case it can't be released).
        self.lock_timeout_ms = lock_timeout_ms
        self.pub = redis.Redis.from_url(url, socket_timeout=1.0, socket_connect_timeout=1.0)
        self.sub = redis.Redis.from_url(url, socket_connect_timeout=1.0)
        self.redlock = Redlock([self.pub], retry_count=0)
        self.locks: dict[str, object] = {}
        self.server = None
        self._stop = threading.Event()

    # ── names ──────────────────────────────────────────────────────────────────

    def pub_key(self, document_name: str) -> str:
        return f"{self.prefix}:{document_name}"

    def lock_key(self, document_name: str) -> str:
        return f"{self.pub_key(document_name)}:lock"

    # ── lifecycle ──────────────────────────────────────────────────────────────

    def on_configure(self, server) -> None:
        self.server = server
        threading.Thread(target=self._subscriber, name="valkey-subscriber", daemon=True).start()

    def _subscriber(self) -> None:
        """Receive other instances' updates. Reconnects for as long as the process runs."""
        while not self._stop.is_set():
            pubsub = None
            try:
                pubsub = self.sub.pubsub(ignore_subscribe_messages=True)
                pubsub.psubscribe(f"{self.prefix}:*")
                while not self._stop.is_set():
                    message = pubsub.get_message(timeout=1.0)
                    if message and message.get("type") == "pmessage":
                        self._on_message(message["channel"].decode(), message["data"])
            except (redis.ConnectionError, redis.TimeoutError, OSError) as error:
                log.warning("valkey subscriber disconnected: %s", error)
                time.sleep(0.5)
            finally:
                if pubsub is not None:
                    try:
                        pubsub.close()
                    except Exception:
                        pass

    def _on_message(self, channel: str, data: bytes) -> None:
        if channel.endswith(":lock"):
            return
        try:
            message = json.loads(data)
        except ValueError:
            return
        if message.get("from") == self.instance_id:
            return
        document_name = channel[len(self.prefix) + 1 :]
        self.server.apply_remote_update(document_name, base64.b64decode(message["update"]))

    # ── hooks ──────────────────────────────────────────────────────────────────

    def on_change(self, payload) -> None:
        """Publish a local update to the other instances (best effort)."""
        if payload.origin != "local":
            return
        message = json.dumps({"from": self.instance_id, "update": base64.b64encode(payload.update).decode()})
        try:
            self.pub.publish(self.pub_key(payload.document_name), message)
        except (redis.ConnectionError, redis.TimeoutError, OSError) as error:
            log.warning("could not publish update for %s: %s", payload.document_name, error)

    def on_store_document(self, payload) -> None:
        """Before the document is stored, make sure to set a lock in Valkey.

        That's meant to avoid conflicts with other instances trying to store the document.
        """
        resource = self.lock_key(payload.document_name)
        ttl = self.lock_timeout_ms
        try:
            lock = self.redlock.acquire([resource], ttl)
            self.locks[resource] = lock
        except ExecutionError as error:
            if str(error) == QUORUM_MESSAGE:
                # Expected behavior: could not acquire the lock, another instance locked it already.
                # Skip further hooks — the data is safe on the other instance.
                log.info("store lock for %s not acquired (%s)", payload.document_name, error.attempts)
                raise SkipFurtherHooks("Another instance is already storing this document")
            log.error("unexpected error: %s", error)
            raise

    def after_store_document(self, payload) -> None:
        """Release the lock, so other instances can store the document."""
        resource = self.lock_key(payload.document_name)
        lock = self.locks.pop(resource, None)
        if lock is not None:
            try:
                lock.release()
            except Exception:
                pass  # the lock expires on its own after lock_timeout_ms
