"""stepq executor: leases due items from the shared queue and runs their steps.

    python -m stepq.executor --id E1

Environment: VALKEY_URL (required), STEP_RUNNER_URL (required), WORKER_SLOTS (default 2),
POLL_MS (default 100). Prints READY on stdout once the function library is loaded.
SIGTERM stops scanning and waits for running steps; SIGKILL may arrive at any time.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass

import redis

from . import layout, library, ulid

# How long one lease (and each extension) lasts. Sized against the shard's worst-case extension
# latency, which runs to several seconds under load: an extension sent at lease/2 must still land
# before the lease it extends runs out. Shortening it has caused duplicate runs.
ITEM_LEASE_MS = 10_000
EXTEND_INTERVAL_MS = ITEM_LEASE_MS // 2
SCAVENGE_INTERVAL_MS = 3_000
# Budget for one lease attempt in the scan loop, so a slow shard can't stall scanning.
LEASE_ATTEMPT_TIMEOUT_S = 1.0
SCAN_BATCH = 10

log = logging.getLogger("stepq.executor")


class LeaseStatus:
    LEASED = "leased"
    CONTENTION = "contention"
    NOT_FOUND = "not_found"
    ERROR = "error"


@dataclass
class Item:
    id: str
    partition: str
    lease_id: str | None = None


class Executor:
    def __init__(self, executor_id: str, valkey_url: str, runner_url: str, slots: int, poll_ms: int):
        self.id = executor_id
        self.runner_url = runner_url.rstrip("/")
        self.poll_s = poll_ms / 1000
        self.slots = threading.BoundedSemaphore(slots)
        self.valkey = redis.Redis.from_url(valkey_url, decode_responses=True)
        self.lease_valkey = redis.Redis.from_url(
            valkey_url, decode_responses=True, socket_timeout=LEASE_ATTEMPT_TIMEOUT_S
        )
        self.stopping = threading.Event()
        self._workers: list[threading.Thread] = []

    # ── main loop ──────────────────────────────────────────────────────────────

    def run(self) -> None:
        library.load(self.valkey)
        print("READY", flush=True)
        threading.Thread(target=self.scavenge_loop, name="scavenger", daemon=True).start()
        while not self.stopping.is_set():
            try:
                self.scan()
            except redis.RedisError as exc:
                log.warning("scan failed: %r", exc)
            self.stopping.wait(self.poll_s)
        for worker in list(self._workers):
            worker.join()

    def scan(self) -> None:
        now = ulid.now_ms()
        for key in self.valkey.scan_iter(match=layout.READY_PATTERN, count=100):
            partition = layout.partition_of_ready_key(key)
            for item_id in self.valkey.zrangebyscore(key, "-inf", now, start=0, num=SCAN_BATCH):
                if not self.slots.acquire(blocking=False):
                    return
                self.lease_item(Item(item_id, partition))

    # ── leasing ────────────────────────────────────────────────────────────────

    def lease_item(self, item: Item) -> str:
        """Lease one due item and hand it to a worker. The caller holds a worker slot."""
        now = ulid.now_ms()
        lease_id = ulid.new(now + ITEM_LEASE_MS)
        try:
            status = self.lease_valkey.fcall(
                "q_lease", 4, *layout.lease_keys(item.partition), item.id, item.partition, lease_id, now
            )
        except redis.RedisError as exc:
            # If the item isn't handed to a worker, the slot must always be released.
            self.slots.release()
            log.warning("lease call failed item=%s lease_id=%s err=%r", item.id, item.lease_id, exc)
            return LeaseStatus.ERROR

        if status == -2:
            # Another executor holds it. This is an okay error: move to the next item.
            self.slots.release()
            return LeaseStatus.CONTENTION
        if status == -1:
            self.slots.release()
            return LeaseStatus.NOT_FOUND
        if status != 0:
            self.slots.release()
            log.warning("unexpected lease status item=%s status=%r", item.id, status)
            return LeaseStatus.ERROR

        item.lease_id = lease_id
        worker = threading.Thread(target=self.process, args=(item,), name=f"step-{item.id}", daemon=True)
        self._workers = [w for w in self._workers if w.is_alive()] + [worker]
        worker.start()
        return LeaseStatus.LEASED

    # ── running a step ─────────────────────────────────────────────────────────

    def process(self, item: Item) -> None:
        try:
            done = threading.Event()
            threading.Thread(target=self.extend_loop, args=(item, done), name=f"extend-{item.id}", daemon=True).start()
            ok = self.run_step(item)
            done.set()
            if ok:
                self.complete(item)
            else:
                self.requeue(item, ulid.now_ms())
        except Exception:  # noqa: BLE001 - a worker must always give its slot back
            log.exception("worker failed item=%s", item.id)
        finally:
            self.slots.release()

    def extend_loop(self, item: Item, done: threading.Event) -> None:
        """Keep the lease alive while the step runs, rotating it every EXTEND_INTERVAL_MS."""
        while not done.wait(EXTEND_INTERVAL_MS / 1000):
            current = item.lease_id
            if current is None:
                log.error("cannot extend lease since lease ID is nil item=%s", item.id)
                return
            next_id = ulid.new(ulid.now_ms() + ITEM_LEASE_MS)
            try:
                status = self.valkey.fcall(
                    "q_extend", 3, *layout.extend_keys(item.partition), item.id, current, next_id, item.partition
                )
            except redis.RedisError as exc:
                log.error("error extending lease item=%s err=%r", item.id, exc)
                return
            if status != 0:
                log.error("lease extension rejected item=%s status=%r", item.id, status)
                return
            item.lease_id = next_id

    def run_step(self, item: Item) -> bool:
        body = json.dumps({"item_id": item.id, "lease_id": item.lease_id, "executor_id": self.id}).encode()
        request = urllib.request.Request(
            f"{self.runner_url}/run", data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(request) as response:
                return 200 <= response.status < 300
        except (urllib.error.URLError, OSError) as exc:
            log.warning("step failed item=%s err=%r", item.id, exc)
            return False

    def complete(self, item: Item) -> None:
        try:
            self.valkey.fcall(
                "q_dequeue", 3, *layout.dequeue_keys(item.partition), item.id, item.partition, item.lease_id
            )
        except redis.RedisError as exc:
            log.error("dequeue failed item=%s err=%r", item.id, exc)

    def requeue(self, item: Item, run_at_ms: int) -> None:
        try:
            self.valkey.fcall(
                "q_requeue", 4, *layout.requeue_keys(item.partition), item.id, item.partition, item.lease_id, run_at_ms
            )
        except redis.RedisError as exc:
            log.error("requeue failed item=%s err=%r", item.id, exc)

    # ── scavenger ──────────────────────────────────────────────────────────────

    def scavenge_loop(self) -> None:
        while not self.stopping.wait(SCAVENGE_INTERVAL_MS / 1000):
            try:
                count = self.scavenge()
            except redis.RedisError as exc:
                log.warning("scavenge failed: %r", exc)
                continue
            if count:
                log.info("scavenged lost items count=%d", count)

    def scavenge(self) -> int:
        """Requeue items whose lease expired, e.g. because their executor died."""
        now = ulid.now_ms()
        count = 0
        for partition in self.valkey.zrangebyscore(layout.CONCURRENCY, "-inf", now, start=0, num=100):
            for item_id in self.valkey.zrangebyscore(layout.scavenger(partition), "-inf", now, start=0, num=100):
                status = self.valkey.fcall(
                    "q_scavenge", 4, *layout.scavenge_keys(partition), item_id, partition, now
                )
                if status == 0:
                    count += 1
        return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m stepq.executor")
    parser.add_argument("--id", required=True, help="executor id, e.g. E1")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format=f"%(asctime)s {args.id} %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    executor = Executor(
        args.id,
        os.environ["VALKEY_URL"],
        os.environ["STEP_RUNNER_URL"],
        int(os.environ.get("WORKER_SLOTS", "2")),
        int(os.environ.get("POLL_MS", "100")),
    )
    signal.signal(signal.SIGTERM, lambda *_: executor.stopping.set())
    executor.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
