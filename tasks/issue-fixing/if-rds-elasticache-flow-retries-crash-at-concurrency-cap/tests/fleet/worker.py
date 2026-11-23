"""The worker: submits due runs and starts a supervisor for each one it gets a slot for.

Every WORKER_POLL_S it lists SCHEDULED runs whose scheduled time has passed, oldest first, and
proposes PENDING for each run it is not already executing. An accepted PENDING means the run's
deployment slot is secured: the worker starts `python -m fleet.supervisor <run id>`. A rejection
(AwaitingConcurrencySlot) is simply picked up again once it is due.

Run with `python -m fleet.worker` (PG_DSN, ORCH_URL, WORKER_POLL_S).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import psycopg

from .common import PG_DSN, WORKER_POLL_S, log, set_state

ROLE = "worker"


def due_runs(conn: psycopg.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT id FROM flow_run
         WHERE state_type = 'SCHEDULED' AND (scheduled_time IS NULL OR scheduled_time <= clock_timestamp())
         ORDER BY created_seq
        """
    ).fetchall()
    return [str(r[0]) for r in rows]


def main() -> int:
    conn = psycopg.connect(PG_DSN, autocommit=True, connect_timeout=5)
    active: dict[str, subprocess.Popen] = {}
    log(ROLE, f"polling every {WORKER_POLL_S}s")
    while True:
        started = time.monotonic()
        for run_id, proc in list(active.items()):
            if proc.poll() is not None:
                log(ROLE, f"supervisor for {run_id} exited with {proc.returncode}",
                    ev="exit", run=run_id, supervisor=proc.pid, code=proc.returncode)
                del active[run_id]
        try:
            runs = due_runs(conn)
        except psycopg.Error as exc:
            log(ROLE, f"poll failed: {exc}; reconnecting")
            conn = psycopg.connect(PG_DSN, autocommit=True, connect_timeout=5)
            runs = []
        for run_id in runs:
            if run_id in active:
                continue  # in-process retry: the supervisor we started still owns this run
            try:
                result = set_state(run_id, "PENDING")
            except Exception as exc:  # noqa: BLE001
                log(ROLE, f"could not submit {run_id}: {exc}")
                continue
            if result["status"] != "ACCEPT":
                log(ROLE, f"{run_id} not submitted: {result['status']} {result['state']['name']}")
                continue
            proc = subprocess.Popen([sys.executable, "-m", "fleet.supervisor", run_id],
                                    start_new_session=True, env=os.environ.copy())
            active[run_id] = proc
            log(ROLE, f"submitted {run_id} (lease {result['state']['lease_id']}), supervisor {proc.pid}",
                ev="spawn", run=run_id, supervisor=proc.pid, lease=result["state"]["lease_id"])
        time.sleep(max(0.0, WORKER_POLL_S - (time.monotonic() - started)))


if __name__ == "__main__":
    sys.exit(main())
