"""One worker: fetch a job, run it, take it out of the pool's in-progress list (worker.go)."""

from __future__ import annotations

import subprocess
import threading
import uuid

import redis

from . import keys, scripts
from .config import Config
from .job import Job
from .log import log_error

# Delay before the next fetch after 0, 1, 2, ... consecutive empty fetches (worker.go uses
# 0/10/100/1000/5000 ms; this deployment keeps idle workers closer to the queue).
SLEEP_BACKOFFS_MS = [0, 10, 100, 250, 500]


class Worker:
    def __init__(self, config: Config, pool_id: str, client: redis.Redis, stopping: threading.Event) -> None:
        self.config = config
        self.pool_id = pool_id
        self.worker_id = uuid.uuid4().hex[:24]
        self.client = client
        self.stopping = stopping
        ns, name = config.namespace, config.job_type
        self.fetch_keys = [
            keys.jobs(ns, name),
            keys.jobs_in_progress(ns, pool_id, name),
            keys.jobs_paused(ns, name),
            keys.jobs_lock(ns, name),
            keys.jobs_lock_info(ns, name),
            keys.jobs_concurrency(ns, name),
        ]
        self.fetch_script = client.register_script(scripts.FETCH_JOB)
        self.thread = threading.Thread(target=self.loop, name=f"worker-{self.worker_id}", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def join(self, timeout: float | None = None) -> None:
        self.thread.join(timeout)

    def loop(self) -> None:
        consecutive_no_jobs = 0
        delay = 0.0
        while not self.stopping.wait(delay):
            try:
                job = self.fetch_job()
            except (redis.RedisError, ValueError) as err:
                log_error("worker.fetch", err)
                delay = 0.010
                continue
            if job is not None:
                self.process_job(job)
                consecutive_no_jobs = 0
                delay = 0.0
            else:
                consecutive_no_jobs += 1
                idx = min(consecutive_no_jobs, len(SLEEP_BACKOFFS_MS) - 1)
                delay = SLEEP_BACKOFFS_MS[idx] / 1000.0

    def fetch_job(self) -> Job | None:
        values = self.fetch_script(keys=self.fetch_keys, args=[self.pool_id])
        if values is None:
            return None
        if len(values) != 3:
            raise ValueError("need 3 elements back")
        raw_json, dequeued_from, in_prog_queue = values
        return Job.parse(raw_json, dequeued_from, in_prog_queue)

    def process_job(self, job: Job) -> None:
        ok = self.run_job(job)
        if not ok:
            log_error("worker.process_job", f"job {job.id} failed")
        self.remove_job_from_in_progress(job)

    def run_job(self, job: Job) -> bool:
        duration_ms = int(job.args.get("duration_ms", 0))
        argv = [self.config.jobrun, "--job-id", job.id, "--pool-id", self.pool_id, "--duration-ms", str(duration_ms)]
        try:
            return subprocess.run(argv, stdin=subprocess.DEVNULL).returncode == 0
        except OSError as err:
            log_error("worker.run_job", err)
            return False

    def remove_job_from_in_progress(self, job: Job) -> None:
        ns, name = self.config.namespace, job.name
        # remove job from in progress and decr the lock in one transaction
        pipe = self.client.pipeline(transaction=True)
        pipe.lrem(job.in_prog_queue, 1, job.raw_json)
        pipe.decr(keys.jobs_lock(ns, name))
        pipe.hincrby(keys.jobs_lock_info(ns, name), self.pool_id, -1)
        try:
            pipe.execute()
        except redis.RedisError as err:
            log_error("worker.remove_job_from_in_progress.lrem", err)
