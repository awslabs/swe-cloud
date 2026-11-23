"""One worker: fetch a job, run it, take it out of the pool's in-progress list (worker.go)."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import uuid

import redis

from . import keys, scripts
from .config import Config
from .job import Job
from .log import log_error

# Delay before the next fetch after 0, 1, 2, ... consecutive empty fetches (worker.go uses
# 0/10/100/1000/5000 ms; this deployment keeps idle workers closer to the queue).
SLEEP_BACKOFFS_MS = [0, 10, 100, 250, 500]


RETRY_DELAY_S = 0.05
APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Claims:
    """The jobs this pool's workers hold. Claims are taken under `lock`, so while it is held every
    entry of the pool's in-progress list that is not listed here belongs to no worker."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.held: dict[bytes, int] = {}

    def add(self, raw_json: bytes) -> None:
        self.held[raw_json] = self.held.get(raw_json, 0) + 1

    def discard(self, raw_json: bytes) -> None:
        with self.lock:
            left = self.held.get(raw_json, 0) - 1
            if left > 0:
                self.held[raw_json] = left
            else:
                self.held.pop(raw_json, None)


class Worker:
    def __init__(self, config: Config, pool_id: str, client: redis.Redis, stopping: threading.Event, claims: Claims) -> None:
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
        self.release_script = client.register_script(scripts.RELEASE_JOB)
        self.claims = claims
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
        with self.claims.lock:
            try:
                values = self.fetch_script(keys=self.fetch_keys, args=[self.pool_id])
            except (redis.ConnectionError, redis.TimeoutError):
                # the claim may have been made with its reply lost: give back what nobody holds
                self.requeue_unheld()
                raise
            if values is None:
                return None
            if len(values) != 3:
                raise ValueError("need 3 elements back")
            raw_json, dequeued_from, in_prog_queue = values
            self.claims.add(raw_json)
        return Job.parse(raw_json, dequeued_from, in_prog_queue)

    def requeue_unheld(self) -> None:
        """Put back every in-progress entry of this pool that no worker holds. Caller holds claims.lock."""
        in_prog_queue = self.fetch_keys[1]
        while not self.stopping.is_set():
            try:
                for raw_json in self.client.lrange(in_prog_queue, 0, -1):
                    if raw_json not in self.claims.held:
                        self.release(in_prog_queue, raw_json, requeue=True)
                return
            except (redis.ConnectionError, redis.TimeoutError) as err:
                log_error("worker.requeue_unheld", err)
                time.sleep(RETRY_DELAY_S)

    def release(self, in_prog_queue: bytes | str, raw_json: bytes, requeue: bool = False) -> int:
        ns, name = self.config.namespace, self.config.job_type
        return self.release_script(
            keys=[in_prog_queue, keys.jobs_lock(ns, name), keys.jobs_lock_info(ns, name), keys.jobs(ns, name),
                  keys.jobs_runners(ns, self.pool_id, name)],
            args=[raw_json, self.pool_id, "1" if requeue else ""],
        )

    def process_job(self, job: Job) -> None:
        if not self.run_job(job):
            # the shim gives the unit back itself; if it could not, the claim must not stay behind
            self.remove_job_from_in_progress(job)
        self.claims.discard(job.raw_json)

    def run_job(self, job: Job) -> bool:
        """Run the job under its shim (work/shim.py), which holds the claim while the runner lives and
        releases it when the runner exits, even if this pool process is gone by then."""
        duration_ms = int(job.args.get("duration_ms", 0))
        argv = [sys.executable, "-m", "work.shim", "--pool-id", self.pool_id, "--job-id", job.id,
                "--duration-ms", str(duration_ms), "--raw-json", job.raw_json.decode()]
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [APP_DIR, os.environ.get("PYTHONPATH")]))}
        try:
            return subprocess.run(argv, stdin=subprocess.DEVNULL, env=env).returncode == 0
        except OSError as err:
            log_error("worker.run_job", err)
            return False

    def remove_job_from_in_progress(self, job: Job) -> None:
        # the decrement is tied to this call's own removal of the entry, so re-sending after an
        # unanswered attempt cannot give the unit back twice
        while True:
            try:
                self.release(job.in_prog_queue, job.raw_json)
                return
            except redis.RedisError as err:
                log_error("worker.remove_job_from_in_progress.lrem", err)
                time.sleep(RETRY_DELAY_S)
