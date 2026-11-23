"""The supervisor (`flow-run execute`): runs one flow run's engine as a child process.

On SIGTERM (node drain, deploy) it reschedules the run: it proposes AwaitingRetry for the run so a
worker picks it up again, then kills the engine and exits.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from datetime import datetime, timezone

from .common import log, set_state

ROLE = "supervisor"


def main(run_id: str) -> int:
    engine = subprocess.Popen([sys.executable, "-m", "fleet.engine", run_id])
    log(ROLE, f"engine {engine.pid} started for {run_id}", ev="engine", run=run_id, engine=engine.pid)

    def reschedule(signum, frame):  # noqa: ARG001
        log(ROLE, f"SIGTERM received, rescheduling {run_id}", ev="sigterm", run=run_id)
        try:
            result = set_state(run_id, "SCHEDULED", "AwaitingRetry",
                               scheduled_time=datetime.now(timezone.utc).isoformat())
            log(ROLE, f"reschedule {result['status']} -> {result['state']['name']}",
                ev="rescheduled", run=run_id, status=result["status"])
        except Exception as exc:  # noqa: BLE001
            log(ROLE, f"failed to reschedule {run_id}: {exc}")
        engine.kill()
        log(ROLE, f"engine {engine.pid} killed", ev="engine_killed", run=run_id, engine=engine.pid)
        # The main thread is blocked in engine.wait(); leave from here rather than wait again.
        os._exit(0)

    signal.signal(signal.SIGTERM, reschedule)
    return engine.wait()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
