"""A job's shim (`python -m work.shim`): starts the runner and holds the job's claim for exactly as long
as the runner lives.

The pool starts one per job. A shim is not the pool process, so it outlives a pool process the memory
guard killed (OPERATIONS.md): it keeps the claim's lease fresh while the runner works, and gives the
unit back itself when the runner exits, whether or not its pool is still there to see it."""

from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time

import redis

from . import keys, scripts
from .config import Config
from .conn import connect
from .log import log_error

LEASE_MS = 3000
RENEW_S = 0.5
RETRY_DELAY_S = 0.05


def until_answered(where: str, call):
    """Re-send call until Valkey answers it. Only for scripts that can be re-sent safely."""
    while True:
        try:
            return call()
        except redis.RedisError as err:
            log_error(where, err)
            time.sleep(RETRY_DELAY_S)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool-id", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--duration-ms", required=True)
    parser.add_argument("--raw-json", required=True)
    args = parser.parse_args()
    config = Config()
    client = connect(config)
    ns, name, pool_id = config.namespace, config.job_type, args.pool_id
    raw_json = args.raw_json.encode()
    in_prog_queue = keys.jobs_in_progress(ns, pool_id, name)
    runners = keys.jobs_runners(ns, pool_id, name)
    hold_script = client.register_script(scripts.HOLD_RUNNER)
    release_script = client.register_script(scripts.RELEASE_JOB)

    def hold() -> int:
        return hold_script(keys=[in_prog_queue, runners], args=[raw_json, LEASE_MS])

    # the lease is taken before the runner starts: a claim a reaper already put back must not run here
    if not until_answered("shim.hold", hold):
        log_error("shim.hold", f"claim on job {args.job_id} was given back before its runner started")
        return 0
    argv = [config.jobrun, "--job-id", args.job_id, "--pool-id", pool_id, "--duration-ms", args.duration_ms]
    try:
        runner = subprocess.Popen(argv, stdin=subprocess.DEVNULL)
    except OSError as err:
        log_error("shim.run_job", err)
        runner = None
    done = threading.Event()

    def renew() -> None:
        while not done.wait(RENEW_S):
            try:
                hold()
            except redis.RedisError as err:
                log_error("shim.renew", err)

    renewer = threading.Thread(target=renew, daemon=True)
    renewer.start()
    if runner is not None and runner.wait() != 0:
        log_error("shim.run_job", f"job {args.job_id} failed")
    done.set()
    renewer.join()
    until_answered(
        "worker.remove_job_from_in_progress.lrem",
        lambda: release_script(
            keys=[in_prog_queue, keys.jobs_lock(ns, name), keys.jobs_lock_info(ns, name), keys.jobs(ns, name), runners],
            args=[raw_json, pool_id, ""],
        ),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
