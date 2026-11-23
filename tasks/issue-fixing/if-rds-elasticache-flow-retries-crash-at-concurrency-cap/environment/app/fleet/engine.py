"""The flow engine: executes one flow run, attempt by attempt.

The engine proposes RUNNING, then runs the flow body under a lease maintainer for the deployment
concurrency lease named on the state the attempt started with: a thread renews it immediately and
then every LEASE_DURATION_S * 0.75. If a renewal is refused (410 Gone) the slot is no longer
reserved, so the engine stops the attempt to prevent over-allocation and reports the run Crashed.

When the body fails and the server answers with AwaitingRetry, the engine retries in-process: it
waits until the state's scheduled time (the maintainer of the failed attempt keeps renewing while
it waits), proposes RUNNING again, and starts the next attempt with the lease on that new state.

The flow body is synthetic: flow_run.parameters describes each attempt, e.g.
    {"attempts": [{"seconds": 1, "outcome": "fail"}, {"seconds": 16, "outcome": "fail"}],
     "otherwise": {"seconds": 3, "outcome": "complete"}}
"""

from __future__ import annotations

import sys
import threading
from datetime import datetime, timezone

import psycopg

from .common import LEASE_DURATION_S, PG_DSN, RENEWAL_FRACTION, HTTPError, log, parse_time, request, set_state

ROLE = "engine"
_RENEWAL_MAX_ATTEMPTS = 3


class LeaseLost(Exception):
    pass


class LeaseMaintainer:
    def __init__(self, run_id: str, lease_id: str | None):
        self.run_id = run_id
        self.lease_id = lease_id
        self.lost = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "LeaseMaintainer":
        if self.lease_id:
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name=f"lease-renewal-{self.lease_id}")
            self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _renew(self) -> bool:
        delay = 1.0
        for attempt in range(_RENEWAL_MAX_ATTEMPTS):
            try:
                status, _ = request("POST", f"/leases/{self.lease_id}/renew",
                                    {"lease_duration": LEASE_DURATION_S}, timeout=10)
            except (OSError, HTTPError) as exc:
                if attempt == _RENEWAL_MAX_ATTEMPTS - 1:
                    log(ROLE, f"lease renewal for {self.lease_id} failed after {attempt + 1} attempts: {exc}",
                        ev="renew", run=self.run_id, lease=self.lease_id, status="error")
                    return False
                if self._stop.wait(delay):
                    return True
                delay *= 2
                continue
            log(ROLE, f"renewed lease {self.lease_id}: {status}",
                ev="renew", run=self.run_id, lease=self.lease_id, status=status)
            return status == 204
        return False

    def _loop(self) -> None:
        while not self._stop.is_set():
            if not self._renew():
                if not self._stop.is_set():
                    log(ROLE, "Concurrency lease renewal failed - slots are no longer reserved. "
                              f"Terminating execution to prevent over-allocation. Lease ID: {self.lease_id}",
                        ev="lease_lost", run=self.run_id, lease=self.lease_id)
                    self.lost.set()
                return
            self._stop.wait(LEASE_DURATION_S * RENEWAL_FRACTION)

    def sleep(self, seconds: float) -> None:
        """Sleep, but stop as soon as the lease is lost."""
        if self.lost.wait(max(0.0, seconds)):
            raise LeaseLost(self.lease_id)


def _plan(conn: psycopg.Connection, run_id: str, attempt: int) -> tuple[float, str]:
    row = conn.execute("SELECT parameters FROM flow_run WHERE id = %s", (run_id,)).fetchone()
    params = (row[0] if row else None) or {}
    attempts = params.get("attempts") or []
    step = attempts[attempt - 1] if 0 < attempt <= len(attempts) else params.get("otherwise")
    step = step or {"seconds": 1, "outcome": "complete"}
    return float(step.get("seconds", 1)), str(step.get("outcome", "complete"))


def _run_body(conn, maintainer: LeaseMaintainer, run_id: str, attempt: int, lease_id: str | None) -> str:
    seconds, outcome = _plan(conn, run_id, attempt)
    row = conn.execute(
        "INSERT INTO attempt_log (flow_run_id, attempt, lease_id) VALUES (%s, %s, %s) RETURNING id",
        (run_id, attempt, lease_id),
    ).fetchone()
    log(ROLE, f"attempt {attempt} of {run_id} started (lease {lease_id}, {seconds}s, {outcome})",
        ev="attempt", run=run_id, attempt=attempt, lease=lease_id)
    result = outcome
    try:
        maintainer.sleep(seconds)
    except LeaseLost:
        result = "cancelled"
        raise
    finally:
        conn.execute("UPDATE attempt_log SET body_end = clock_timestamp(), outcome = %s WHERE id = %s",
                     (result, row[0]))
    return outcome


def main(run_id: str) -> int:
    conn = psycopg.connect(PG_DSN, autocommit=True, connect_timeout=5)
    response = set_state(run_id, "RUNNING")
    if response["status"] != "ACCEPT":
        log(ROLE, f"{run_id} not started: {response['status']} {response.get('reason')}")
        return 1

    while True:
        state = response["state"]
        attempt = response["run"]["run_count"]
        lease_id = state.get("lease_id")
        with LeaseMaintainer(run_id, lease_id) as maintainer:
            try:
                outcome = _run_body(conn, maintainer, run_id, attempt, lease_id)
                if outcome == "complete":
                    result = set_state(run_id, "COMPLETED")
                    log(ROLE, f"{run_id} completed: {result['status']}")
                    return 0

                result = set_state(run_id, "FAILED", message=f"attempt {attempt} raised")
                log(ROLE, f"{run_id} failure reported: {result['status']} -> {result['state']['name']}")
                if not (result["status"] == "REJECT" and result["state"]["type"] == "SCHEDULED"):
                    return 1

                # In-process retry: wait for the retry time inside this attempt's lease maintainer.
                due = parse_time(result["state"]["scheduled_time"]) or datetime.now(timezone.utc)
                maintainer.sleep((due - datetime.now(timezone.utc)).total_seconds())
                response = set_state(run_id, "RUNNING")
                if response["status"] != "ACCEPT":
                    log(ROLE, f"{run_id} retry not started: {response['status']} {response.get('reason')}")
                    return 1
            except LeaseLost:
                result = set_state(run_id, "CRASHED", message="Concurrency lease renewal failed")
                log(ROLE, f"{run_id} reported crashed: {result['status']}")
                return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
