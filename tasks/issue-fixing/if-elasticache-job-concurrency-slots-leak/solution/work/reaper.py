"""The dead pool reaper (dead_pool_reaper.go). Every pool runs one."""

from __future__ import annotations

import random
import threading
import time

import redis

from . import keys, scripts
from .config import Config
from .log import log_error


class DeadPoolReaper:
    def __init__(self, config: Config, client: redis.Redis) -> None:
        self.config = config
        self.client = client
        self.cur_job_types = [config.job_type]
        self.reap_script = client.register_script(scripts.REAP_DEAD_POOL)
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.loop, name="dead-pool-reaper", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stopping.set()
        self.thread.join()

    def loop(self) -> None:
        # Reap immediately after we provide some time for initialization
        delay = self.config.dead_time_s
        while not self.stopping.wait(delay):
            # Schedule next occurrence periodically with jitter
            delay = self.config.reap_period_s + random.randrange(max(1, self.config.reap_jitter_s))
            try:
                self.reap()
            except redis.RedisError as err:
                log_error("dead_pool_reaper.reap", err)

    def reap(self) -> None:
        ns = self.config.namespace
        for dead_pool_id, job_types in self.find_dead_pools().items():
            # with no heartbeat left, clean up for the current set of jobs
            for job_type in job_types or self.cur_job_types:
                self.reap_script(
                    keys=[
                        keys.jobs_in_progress(ns, dead_pool_id, job_type),
                        keys.jobs(ns, job_type),
                        keys.jobs_lock(ns, job_type),
                        keys.jobs_lock_info(ns, job_type),
                        keys.jobs_runners(ns, dead_pool_id, job_type),
                        keys.heartbeat(ns, dead_pool_id),
                        keys.worker_pools(ns),
                    ],
                    args=[dead_pool_id, int(time.time()), self.config.dead_time_s],
                )

    def find_dead_pools(self) -> dict[str, list[str]]:
        ns = self.config.namespace
        dead_pools: dict[str, list[str]] = {}
        for raw_id in self.client.smembers(keys.worker_pools(ns)):
            pool_id = raw_id.decode()
            heartbeat_key = keys.heartbeat(ns, pool_id)
            heartbeat_at = self.client.hget(heartbeat_key, "heartbeat_at")
            if heartbeat_at is None:
                # heartbeat expired, save dead pool and use cur set of jobs from reaper
                dead_pools[pool_id] = []
                continue
            # Check that last heartbeat was long enough ago to consider the pool dead
            if int(heartbeat_at) + self.config.dead_time_s > time.time():
                continue
            job_names = self.client.hget(heartbeat_key, "job_names")
            if job_names is None:
                continue
            dead_pools[pool_id] = job_names.decode().split(",")
        return dead_pools
