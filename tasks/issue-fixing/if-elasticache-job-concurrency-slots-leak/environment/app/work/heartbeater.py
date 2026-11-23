"""The pool's heartbeat (heartbeater.go): a hash with no TTL, refreshed every beat period."""

from __future__ import annotations

import os
import socket
import threading
import time

import redis

from . import keys
from .config import Config
from .log import log_error


def now_epoch_seconds() -> int:
    return int(time.time())


class Heartbeater:
    def __init__(self, config: Config, pool_id: str, client: redis.Redis, concurrency: int, worker_ids: list[str]) -> None:
        self.config = config
        self.pool_id = pool_id
        self.client = client
        self.concurrency = concurrency
        self.job_names = ",".join(sorted([config.job_type]))
        self.worker_ids = ",".join(sorted(worker_ids))
        self.started_at = now_epoch_seconds()
        self.pid = os.getpid()
        try:
            self.hostname = socket.gethostname()
        except OSError as err:
            log_error("heartbeat.hostname", err)
            self.hostname = "hostname_errored"
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.loop, name="heartbeater", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stopping.set()
        self.thread.join()

    def loop(self) -> None:
        while not self.stopping.wait(self.config.heartbeat_period_s):
            self.heartbeat()
        self.remove_heartbeat()

    def heartbeat(self) -> bool:
        ns = self.config.namespace
        pipe = self.client.pipeline(transaction=False)
        pipe.sadd(keys.worker_pools(ns), self.pool_id)
        pipe.hset(
            keys.heartbeat(ns, self.pool_id),
            mapping={
                "heartbeat_at": now_epoch_seconds(),
                "started_at": self.started_at,
                "job_names": self.job_names,
                "concurrency": self.concurrency,
                "worker_ids": self.worker_ids,
                "host": self.hostname,
                "pid": self.pid,
            },
        )
        try:
            pipe.execute()
            return True
        except redis.RedisError as err:
            log_error("heartbeat", err)
            return False

    def remove_heartbeat(self) -> None:
        ns = self.config.namespace
        pipe = self.client.pipeline(transaction=False)
        pipe.srem(keys.worker_pools(ns), self.pool_id)
        pipe.delete(keys.heartbeat(ns, self.pool_id))
        try:
            pipe.execute()
        except redis.RedisError as err:
            log_error("remove_heartbeat", err)
