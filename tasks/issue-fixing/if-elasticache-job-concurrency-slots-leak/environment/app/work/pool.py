"""The worker pool (worker_pool.go): workers, heartbeater and dead pool reaper for one job type."""

from __future__ import annotations

import signal
import sys
import threading
import time

import redis

from . import keys
from .config import Config
from .conn import connect
from .heartbeater import Heartbeater
from .log import log_error, log_info
from .reaper import DeadPoolReaper
from .worker import Worker


class WorkerPool:
    def __init__(self, config: Config, pool_id: str, concurrency: int) -> None:
        self.config = config
        self.pool_id = pool_id
        self.client = connect(config)
        self.stopping = threading.Event()
        self.workers = [Worker(config, pool_id, self.client, self.stopping) for _ in range(concurrency)]
        self.heartbeater = Heartbeater(config, pool_id, self.client, concurrency, [w.worker_id for w in self.workers])
        self.reaper = DeadPoolReaper(config, self.client)

    def write_concurrency_controls(self) -> None:
        self.client.set(keys.jobs_concurrency(self.config.namespace, self.config.job_type), self.config.max_concurrency)

    def start(self) -> None:
        while True:
            try:
                self.write_concurrency_controls()
                break
            except redis.RedisError as err:
                log_error("write_concurrency_controls_max_concurrency", err)
                time.sleep(0.5)
        while not self.heartbeater.heartbeat():
            time.sleep(0.5)
        sys.stdout.write(f"READY {self.pool_id}\n")
        sys.stdout.flush()
        for worker in self.workers:
            worker.start()
        self.heartbeater.start()
        self.reaper.start()
        log_info("worker_pool.start", f"pool {self.pool_id} with {len(self.workers)} workers")

    def stop(self) -> None:
        self.stopping.set()
        for worker in self.workers:
            worker.join()
        self.heartbeater.stop()
        self.reaper.stop()
        log_info("worker_pool.stop", f"pool {self.pool_id} stopped")


def run(config: Config, pool_id: str, concurrency: int) -> int:
    pool = WorkerPool(config, pool_id, concurrency)
    done = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: done.set())
    pool.start()
    while not done.wait(1.0):
        pass
    pool.stop()
    return 0
