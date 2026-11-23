"""Command line: `python3 -m dq add ...` and `python3 -m dq worker ...` (see README.md)."""

import argparse
import json
import logging
import signal
import sys

from .connection import connect
from .queue import Queue
from .worker import Worker, load_handler


def cmd_add(args):
    try:
        data = json.loads(args.data)
    except ValueError as exc:
        raise SystemExit(f"--data is not JSON: {exc}")
    queue = Queue(connect(), args.queue)
    job_id, deduplicated = queue.add(args.name, data, dedup_id=args.dedup_id, ttl_ms=args.ttl_ms,
                                     keep_last=args.keep_last)
    print(json.dumps({"job_id": job_id, "deduplicated": deduplicated}), flush=True)


def cmd_worker(args):
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(process)d %(name)s %(levelname)s %(message)s")
    worker = Worker(connect(), args.queue, load_handler(args.handler),
                    lock_ms=args.lock_ms, lock_renew_ms=args.lock_renew_ms,
                    stalled_interval_ms=args.stalled_interval_ms,
                    max_stalled_count=args.max_stalled_count,
                    attempts=args.attempts, backoff_ms=args.backoff_ms)
    signal.signal(signal.SIGTERM, lambda *_: worker.close())
    worker.run()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python3 -m dq")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="add one job and print one JSON line")
    add.add_argument("--queue", required=True)
    add.add_argument("--name", required=True)
    add.add_argument("--data", default="{}", help="job payload, JSON")
    add.add_argument("--dedup-id", default=None)
    add.add_argument("--ttl-ms", type=int, default=None,
                     help="with --dedup-id: throttle window in ms instead of simple mode")
    add.add_argument("--keep-last", action="store_true",
                     help="with --dedup-id: keep the latest add made while the job runs (README)")
    add.set_defaults(func=cmd_add)

    worker = sub.add_parser("worker", help="process jobs until killed")
    worker.add_argument("--queue", required=True)
    worker.add_argument("--handler", default="handlers:run", help="module:function")
    worker.add_argument("--lock-ms", type=int, default=30000)
    worker.add_argument("--lock-renew-ms", type=int, default=None, help="default: lock-ms / 2")
    worker.add_argument("--stalled-interval-ms", type=int, default=30000)
    worker.add_argument("--max-stalled-count", type=int, default=1)
    worker.add_argument("--attempts", type=int, default=1)
    worker.add_argument("--backoff-ms", type=int, default=1000)
    worker.set_defaults(func=cmd_worker)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
