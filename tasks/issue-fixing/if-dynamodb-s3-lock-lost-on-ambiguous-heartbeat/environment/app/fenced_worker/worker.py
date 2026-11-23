"""fenced_worker: hold the ledger lock and append to the ledger while holding.

Two (or more) of these run against the same lock item; at most one appends at
a time.  See /app/README.md.
"""

import argparse
import json
import logging
import os
import secrets
import signal
import sys
import threading
import time

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from .ledger import Ledger
from .lockclient import LockClient

log = logging.getLogger("fenced_worker")


def parse_args(argv):
    p = argparse.ArgumentParser(prog="python -m fenced_worker")
    p.add_argument("--worker-id", required=True)
    p.add_argument("--endpoint", required=True, help="AWS endpoint for every call (DynamoDB and S3)")
    p.add_argument("--region", default="us-east-1")
    p.add_argument("--table", default="locks")
    p.add_argument("--lock-key", default="ledger-lock")
    p.add_argument("--bucket", default="fenced-ledger")
    p.add_argument("--ledger-key", default="ledger.json")
    p.add_argument("--lease-ms", type=int, default=6000)
    p.add_argument("--heartbeat-ms", type=int, default=1000)
    p.add_argument("--refresh-ms", type=int, default=500)
    p.add_argument("--buffer-ms", type=int, default=1000)
    p.add_argument("--work-interval-ms", type=int, default=500)
    p.add_argument("--journal", required=True, help="append-only file of acknowledged entries")
    return p.parse_args(argv)


class Journal:
    def __init__(self, path):
        self._f = open(path, "a", encoding="utf-8")

    def record(self, entry, etag):
        line = dict(entry)
        line["etag"] = etag
        self._f.write(json.dumps(line, separators=(",", ":")) + "\n")
        self._f.flush()
        os.fsync(self._f.fileno())


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s " + args.worker_id + " %(name)s %(levelname)s %(message)s",
    )

    heartbeat_s = args.heartbeat_ms / 1000.0
    owner = "%s:%s" % (args.worker_id, secrets.token_hex(6))
    ddb = boto3.client(
        "dynamodb",
        endpoint_url=args.endpoint,
        region_name=args.region,
        config=Config(
            connect_timeout=heartbeat_s,
            read_timeout=heartbeat_s,
            retries={"mode": "standard", "max_attempts": 3},
        ),
    )
    ledger = Ledger(args.endpoint, args.region, args.bucket, args.ledger_key)
    journal = Journal(args.journal)
    locks = LockClient(
        ddb,
        args.table,
        owner,
        lease_s=args.lease_ms / 1000.0,
        heartbeat_s=heartbeat_s,
        refresh_s=args.refresh_ms / 1000.0,
        buffer_s=args.buffer_ms / 1000.0,
    )
    locks.start_heartbeats()

    def shutdown():
        # Hand the lock over on the way out, so that during a deploy the
        # standby takes over at its next refresh instead of a lease later.
        locks.close()
        os._exit(0)

    signal.signal(signal.SIGTERM,
                  lambda *_: threading.Thread(target=shutdown, name="shutdown", daemon=True).start())
    print("READY", flush=True)
    log.info("started as %s", owner)

    interval = args.work_interval_ms / 1000.0
    n = 0
    while True:
        lock = locks.acquire(args.lock_key)
        log.info("acquired %s with fence %d (rvn %s)", args.lock_key, lock.fence, lock.rvn)
        while locks.holds(lock):
            started = time.monotonic()
            n += 1
            entry = {"writer": args.worker_id, "fence": lock.fence, "n": n}
            try:
                etag = ledger.append(entry, lock.fence)
            except (ClientError, BotoCoreError) as err:
                log.warning("ledger append %d failed: %s", n, err)
            else:
                journal.record(entry, etag)
            time.sleep(max(0.0, interval - (time.monotonic() - started)))
        log.warning("no longer holding %s (fence %d)", args.lock_key, lock.fence)
