"""Verifier-owned handler, loaded inside the solution's workers (--handler vh_handler:run).

Records start / end / error events of every run (with the payload's "rev", if any) to a stream and
obeys a per-dedup-id gate:
"hang" polls until the gate changes, "fail" flips the gate back to "hang" and raises once,
anything else (or no gate) returns.
"""

import os
import time

import redis

_conn = None


def _redis():
    global _conn
    if _conn is None:
        _conn = redis.Redis.from_url(os.environ["DQ_REDIS_URL"], decode_responses=True)
    return _conn


def _event(r, ev, job_id, deid, attempt, rev):
    r.xadd(os.environ["VH_RUNS_KEY"], {"ev": ev, "job": job_id, "deid": deid, "pid": os.getpid(),
                                       "attempt": attempt, "rev": rev, "ts": int(time.time() * 1000)})


def run(job):
    r = _redis()
    job_id = str(job.get("id"))
    deid = job.get("dedup_id") or r.hget(f"dq:{os.environ['VH_QUEUE']}:job:{job_id}", "deid") or ""
    attempt = str(job.get("attempt", ""))
    data = job.get("data")
    rev = str(data.get("rev", "")) if isinstance(data, dict) else ""
    _event(r, "start", job_id, deid, attempt, rev)
    gate = os.environ["VH_GATE_PREFIX"] + deid
    while True:
        state = r.get(gate)
        if state == "hang":
            time.sleep(0.1)
            continue
        if state == "fail":
            r.set(gate, "hang")
            _event(r, "error", job_id, deid, attempt, rev)
            raise RuntimeError("timeout acquiring a connection from the pool")
        break
    _event(r, "end", job_id, deid, attempt, rev)
    return {"ok": True}
