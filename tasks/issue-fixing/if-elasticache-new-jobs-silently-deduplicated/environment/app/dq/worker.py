import importlib
import json
import logging
import os
import threading
import time
import traceback
import uuid

from .keys import QueueKeys
from .queue import now_ms
from .scripts import Scripts

log = logging.getLogger("dq.worker")


def load_handler(spec):
    module_name, _, attr = spec.partition(":")
    return getattr(importlib.import_module(module_name), attr or "run")


class Worker:
    """Processes one job at a time.

    While a handler runs, a renewal thread extends the job's lock every
    lock_renew_ms. Every worker also runs the stall checker every
    stalled_interval_ms; a job whose worker died (no lock) is moved back to
    wait, or failed once it stalled more than max_stalled_count times.
    A handler that raises is a failed attempt: it is retried after
    backoff_ms * 2**(attempts made - 1) until `attempts` attempts are made.
    """

    def __init__(self, conn, queue, handler, lock_ms=30000, lock_renew_ms=None,
                 stalled_interval_ms=30000, max_stalled_count=1, attempts=1, backoff_ms=1000,
                 poll_ms=50):
        self.conn = conn
        self.keys = QueueKeys(queue)
        self.scripts = Scripts(conn)
        self.handler = handler
        self.lock_ms = int(lock_ms)
        self.lock_renew_ms = int(lock_renew_ms or self.lock_ms // 2)
        self.stalled_interval_ms = int(stalled_interval_ms)
        self.max_stalled_count = int(max_stalled_count)
        self.attempts = int(attempts)
        self.backoff_ms = int(backoff_ms)
        self.poll_ms = int(poll_ms)
        self.worker_id = uuid.uuid4().hex
        self._closing = threading.Event()

    # ---- stall checker -------------------------------------------------------------------

    def check_stalled(self):
        k = self.keys
        return self.scripts.moveStalledJobsToWait(
            keys=[k.stalled, k.wait, k.active, k.stalled_check, k.failed, k.events],
            args=[self.max_stalled_count, k.prefix, now_ms(), self.stalled_interval_ms],
        )

    def _stalled_checker(self):
        while not self._closing.is_set():
            try:
                for job_id in self.check_stalled():
                    log.warning("job %s stalled", job_id)
            except Exception:
                log.exception("stall check failed")
            self._closing.wait(self.stalled_interval_ms / 1000)

    # ---- job lifecycle -------------------------------------------------------------------

    def fetch(self, token):
        k = self.keys
        row = self.scripts.moveToActive(
            keys=[k.wait, k.active, k.delayed, k.events],
            args=[k.prefix, token, self.lock_ms, now_ms()],
        )
        if not row:
            return None
        job_id, name, data, deid, atm, stc = row
        return {"id": str(job_id), "name": name, "data": json.loads(data),
                "dedup_id": deid or None, "attempts_made": int(atm), "stalled_count": int(stc)}

    def _renew_lock(self, job_id, token, done):
        k = self.keys
        while not done.wait(self.lock_renew_ms / 1000):
            try:
                if not self.scripts.extendLock(keys=[k.lock(job_id), k.stalled],
                                               args=[token, self.lock_ms, job_id]):
                    log.warning("lost the lock of job %s", job_id)
                    return
            except Exception:
                log.exception("could not extend the lock of job %s", job_id)

    def _finish(self, job_id, token, target, field, value):
        k = self.keys
        zset = k.completed if target == "completed" else k.failed
        return self.scripts.moveToFinished(
            keys=[k.active, k.stalled, zset, k.events],
            args=[k.prefix, job_id, token, now_ms(), target, field, value],
        )

    def _retry_later(self, job_id, token, delay_ms, reason):
        k = self.keys
        return self.scripts.moveToDelayed(
            keys=[k.active, k.stalled, k.delayed, k.events],
            args=[k.prefix, job_id, token, now_ms(), delay_ms, reason],
        )

    def process(self, job, token):
        job_id = job["id"]
        done = threading.Event()
        renewer = threading.Thread(target=self._renew_lock, args=(job_id, token, done), daemon=True)
        renewer.start()
        try:
            result = self.handler({"id": job_id, "name": job["name"], "data": job["data"],
                                   "dedup_id": job["dedup_id"],
                                   "attempt": job["attempts_made"] + 1})
            error = None
        except Exception as exc:
            error = exc
            log.warning("job %s attempt %d failed: %s", job_id, job["attempts_made"] + 1,
                        "".join(traceback.format_exception_only(type(exc), exc)).strip())
        finally:
            done.set()
            renewer.join()

        if error is None:
            code = self._finish(job_id, token, "completed", "returnvalue", json.dumps(result))
        else:
            attempts_made = job["attempts_made"] + 1
            reason = str(error) or type(error).__name__
            if attempts_made < self.attempts:
                delay = self.backoff_ms * 2 ** (attempts_made - 1)
                code = self._retry_later(job_id, token, delay, reason)
            else:
                code = self._finish(job_id, token, "failed", "failedReason", reason)
        if code != 0:
            log.warning("could not move job %s out of active (code %s)", job_id, code)

    # ---- main loop -----------------------------------------------------------------------

    def run(self):
        self.conn.ping()
        checker = threading.Thread(target=self._stalled_checker, daemon=True)
        checker.start()
        print(f"READY {os.getpid()}", flush=True)
        seq = 0
        while not self._closing.is_set():
            seq += 1
            token = f"{self.worker_id}:{seq}"
            try:
                job = self.fetch(token)
            except Exception:
                log.exception("fetch failed")
                time.sleep(1)
                continue
            if job is None:
                time.sleep(self.poll_ms / 1000)
                continue
            self.process(job, token)

    def close(self):
        self._closing.set()
