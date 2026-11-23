"""Outbox relay: publishes committed outbox rows to the ledger-events FIFO queue in position order.

Run as `python3 /app/relay.py`. Connection settings come from the environment: PGHOST, PGPORT,
PGDATABASE, PGUSER, PGPASSWORD for Postgres; OUTBOX_QUEUE_URL, AWS_ENDPOINT_URL and AWS_REGION for
SQS. Prints READY once it is connected and has read its checkpoint. SIGTERM stops it after the
current poll.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from outbox_relay import config  # noqa: E402
from outbox_relay.frontier import FrontierTracker  # noqa: E402
from outbox_relay.publisher import Publisher  # noqa: E402
from outbox_relay.store import CheckpointStore, OutboxSource, connect  # noqa: E402

log = logging.getLogger("relay")


def main() -> int:
    logging.basicConfig(level=os.environ.get("RELAY_LOG_LEVEL", "INFO"), stream=sys.stderr,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = config.load()
    conn = connect()
    source, checkpoint = OutboxSource(conn), CheckpointStore(conn)
    publisher = Publisher(cfg.queue_url, cfg.group_id)
    tracker = FrontierTracker(cfg.stale_gap_seconds)
    position, _ = checkpoint.read()
    log.info("starting at checkpoint %d", position)

    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print("READY", flush=True)

    while not stopping:
        rows = source.read_after(position, cfg.batch_limit)
        plan = tracker.plan(position, rows)
        if plan.rows:
            publisher.publish(plan.rows)
        if plan.frontier > position:
            checkpoint.advance(plan.frontier, {"last_message_id": plan.rows[-1].message_id if plan.rows else None})
            position = plan.frontier
        if len(rows) < cfg.batch_limit:
            time.sleep(cfg.poll_interval_seconds)
    log.info("stopped at checkpoint %d", position)
    return 0


if __name__ == "__main__":
    sys.exit(main())
