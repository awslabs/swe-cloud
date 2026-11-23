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
        self.requeue_script = client.register_script(scripts.REENQUEUE_JOB)
        self.reap_locks_script = client.register_script(scripts.REAP_STALE_LOCKS)
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
        dead_pools = self.find_dead_pools()
        ns = self.config.namespace
        for dead_pool_id, job_types in dead_pools.items():
            lock_job_types = job_types
            # if we found jobs from the heartbeat, requeue them and remove the heartbeat
            if job_types:
                try:
                    self.requeue_in_progress_jobs(dead_pool_id, job_types)
                except (redis.RedisError, ValueError) as err:
                    log_error("dead_pool_reaper.requeue", err)
                self.client.delete(keys.heartbeat(ns, dead_pool_id))
            else:
                # try to clean up locks for the current set of jobs if heartbeat was not found
                lock_job_types = self.cur_job_types
            # Remove dead pool from worker pools set
            self.client.srem(keys.worker_pools(ns), dead_pool_id)
            # Cleanup any stale lock info
            self.clean_stale_lock_info(dead_pool_id, lock_job_types)

    def clean_stale_lock_info(self, pool_id: str, job_types: list[str]) -> None:
        ns = self.config.namespace
        script_keys: list[str] = []
        for job_type in job_types:
            script_keys += [keys.jobs_lock(ns, job_type), keys.jobs_lock_info(ns, job_type)]
        self.reap_locks_script(keys=script_keys, args=[pool_id])

    def requeue_in_progress_jobs(self, pool_id: str, job_types: list[str]) -> None:
        ns = self.config.namespace
        script_keys: list[str] = []
        for job_type in job_types:
            # pops from in progress, push into job queue and decrement the queue lock
            script_keys += [
                keys.jobs_in_progress(ns, pool_id, job_type),
                keys.jobs(ns, job_type),
                keys.jobs_lock(ns, job_type),
                keys.jobs_lock_info(ns, job_type),
            ]
        # Keep moving jobs until all queues are empty
        while True:
            values = self.requeue_script(keys=script_keys, args=[pool_id])
            if values is None:
                return
            if len(values) != 3:
                raise ValueError("need 3 elements back")

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
