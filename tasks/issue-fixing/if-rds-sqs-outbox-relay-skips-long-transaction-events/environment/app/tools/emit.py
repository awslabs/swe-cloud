"""Emit test events through the producer library.

    eval "$(python3 /app/ops/devstack.py env --writer)"
    python3 /app/tools/emit.py --count 1000 --writers 4
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import threading
import time
import uuid

import psycopg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from outbox.writer import OutboxWriter  # noqa: E402


def run(n: int, results: list):
    conn = psycopg.connect(application_name="emit")
    writer = OutboxWriter(conn)
    for _ in range(n):
        with writer.transaction() as tx:
            time.sleep(random.uniform(0.0, 0.03))
            mid = f"emit-{uuid.uuid4()}"
            results.append(tx.append(mid, "TestEvent", {"id": mid, "at": time.time()}))
            time.sleep(random.uniform(0.0, 0.03))
    conn.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--count", type=int, default=100)
    p.add_argument("--writers", type=int, default=1)
    args = p.parse_args()
    results: list = []
    per = [args.count // args.writers + (1 if i < args.count % args.writers else 0) for i in range(args.writers)]
    threads = [threading.Thread(target=run, args=(k, results)) for k in per]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print(f"emitted {len(results)} events, positions {min(results)}..{max(results)}")


if __name__ == "__main__":
    main()
