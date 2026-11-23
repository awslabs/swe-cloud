"""``python3 -m durable_task run ...``: run one try of a task instance.

Exit status: 0 when the task instance's job succeeded (this try ran it, reconnected to it, or found
it already done); 1 when it did not (the job failed, timed out or was stopped, or the try errored).
"""
import argparse
import logging
import sys

from durable_task.job import JobClient, JobRunFailed
from durable_task.resumable import ResumableJob
from durable_task.store import TaskStateStore
from durable_task.ti import TaskInstance

EXIT_SUCCESS = 0
EXIT_FAILED = 1


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="durable_task")
    sub = p.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run one try of a task instance")
    run.add_argument("--dag-id", required=True)
    run.add_argument("--task-id", required=True)
    run.add_argument("--run-id", required=True)
    run.add_argument("--map-index", type=int, default=-1)
    run.add_argument("--try-number", type=int, required=True)
    run.add_argument("--max-tries", type=int, required=True)
    run.add_argument("--state-machine-arn", required=True, help="the job's state machine")
    run.add_argument("--table", default="task_state_store", help="task state store table")
    return p


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("botocore").setLevel(logging.WARNING)
    log = logging.getLogger("durable_task")
    ti = TaskInstance(
        dag_id=args.dag_id,
        task_id=args.task_id,
        run_id=args.run_id,
        map_index=args.map_index,
        try_number=args.try_number,
        max_tries=args.max_tries,
    )
    log.info("try starting: %s", ti)
    runner = ResumableJob(ti, TaskStateStore(args.table, ti), JobClient(args.state_machine_arn))
    try:
        status = runner.execute()
    except JobRunFailed as exc:
        log.error("task failed: %s", exc)
        return EXIT_FAILED
    except Exception:  # noqa: BLE001
        log.exception("task errored")
        return EXIT_FAILED
    log.info("task succeeded (%s)", status)
    return EXIT_SUCCESS
