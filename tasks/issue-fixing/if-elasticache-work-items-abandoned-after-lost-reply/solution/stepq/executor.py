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
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

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
# Pause between attempts of one call whose outcome is unknown.
RETRY_PAUSE_S = 0.05
# Pause before asking the step runner again about a run it gave no answer for.
RUNNER_RETRY_S = 0.2
# Round trips sampled per clock sync; the fastest one bounds the offset's error.
CLOCK_SAMPLES = 5

log = logging.getLogger("stepq.executor")


class LeaseStatus:
    LEASED = "leased"
    CONTENTION = "contention"
    NOT_FOUND = "not_found"
    ERROR = "error"


class ShardClock:
    """The shard's clock as seen from this host: local time plus a measured offset.

    Lease expiries, run-at scores and nowMS are all on the shard's clock, and this host's clock is
    not it. Every "now" the executor compares against the queue comes from here.
    """

    def __init__(self, client: redis.Redis):
        self.client = client
        self.offset_ms = 0

    def sync(self) -> None:
        best = None
        for _ in range(CLOCK_SAMPLES):
            sent = ulid.now_ms()
            seconds, micros = self.client.time()
            back = ulid.now_ms()
            offset = int(seconds) * 1000 + int(micros) // 1000 - (sent + back) // 2
            if best is None or back - sent < best[0]:
                best = (back - sent, offset)
        self.offset_ms = best[1]

    def now_ms(self) -> int:
        return ulid.now_ms() + self.offset_ms


@dataclass
class Item:
    id: str
    partition: str
    lease_id: str | None = None
    # Set when the lease can no longer be kept: someone else may own the item now.
    lost: threading.Event = field(default_factory=threading.Event, repr=False)


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
        self.clock = ShardClock(self.lease_valkey)
        self.stopping = threading.Event()
        self._workers: list[threading.Thread] = []

    # ── main loop ──────────────────────────────────────────────────────────────

    def run(self) -> None:
        library.load(self.valkey)
        while True:
            try:
                self.clock.sync()
                break
            except redis.RedisError as exc:
                log.warning("clock sync failed: %r", exc)
                time.sleep(RETRY_PAUSE_S)
        log.info("shard clock offset_ms=%d", self.clock.offset_ms)
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
        now = self.clock.now_ms()
        for key in self.valkey.scan_iter(match=layout.READY_PATTERN, count=100):
            partition = layout.partition_of_ready_key(key)
            for item_id in self.valkey.zrangebyscore(key, "-inf", now, start=0, num=SCAN_BATCH):
                if self.stopping.is_set() or not self.slots.acquire(blocking=False):
                    return
                self.lease_item(Item(item_id, partition))

    # ── leasing ────────────────────────────────────────────────────────────────

    def lease_item(self, item: Item) -> str:
        """Lease one due item and hand it to a worker. The caller holds a worker slot.

        A lease call that fails in transport may still have committed. The lease ID is ours and
        q_lease recognises it, so the same attempt is repeated under the same ID until Valkey
        answers or that lease would have expired anyway. A SIGTERM doesn't end the attempt: walking
        away from a lease that may have committed strands the item until it expires, and an item we
        do lease is a running step, which a stopping executor still runs to the end.
        """
        lease_id = ulid.new(self.clock.now_ms() + ITEM_LEASE_MS)
        give_up_ms = ulid.time_ms(lease_id) - EXTEND_INTERVAL_MS
        while True:
            try:
                status = self.lease_valkey.fcall(
                    "q_lease", 4, *layout.lease_keys(item.partition), item.id, item.partition, lease_id, self.clock.now_ms()
                )
                break
            except redis.RedisError as exc:
                if self.clock.now_ms() >= give_up_ms:
                    # If the item isn't handed to a worker, the slot must always be released.
                    # A committed lease left behind expires and is scavenged.
                    self.slots.release()
                    log.warning("lease call failed item=%s lease_id=%s err=%r", item.id, lease_id, exc)
                    return LeaseStatus.ERROR
                log.info("lease outcome unknown, retrying item=%s lease_id=%s err=%r", item.id, lease_id, exc)
                time.sleep(RETRY_PAUSE_S)

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
            # The run's name at the step runner. If an earlier holder already started it (and died), this
            # is that run, and the runner attaches us to it rather than running the step a second time.
            run_id = self._call_until(
                ulid.time_ms(item.lease_id), "q_start", 1, layout.ITEMS, item.id, item.lease_id
            )
            if not isinstance(run_id, str):
                log.error("cannot start run item=%s status=%r", item.id, run_id)
                return
            done = threading.Event()
            extender = threading.Thread(target=self.extend_loop, args=(item, done), name=f"extend-{item.id}", daemon=True)
            extender.start()
            ok = self.run_step(item, run_id)
            done.set()
            # An extension in flight may rotate the lease; release under the lease it leaves.
            extender.join()
            if ok is None:
                return
            if ok:
                self.complete(item)
            else:
                self.requeue(item, self.clock.now_ms())
        except Exception:  # noqa: BLE001 - a worker must always give its slot back
            log.exception("worker failed item=%s", item.id)
        finally:
            self.slots.release()

    def extend_loop(self, item: Item, done: threading.Event) -> None:
        """Keep the lease alive while the step runs, rotating it half a lease before it expires.

        The schedule follows the lease's own expiry, not the dispatch time: a lease recovered after
        a lost reply is already older than it looks.
        """
        while True:
            current = item.lease_id
            if current is None:
                log.error("cannot extend lease since lease ID is nil item=%s", item.id)
                return
            wait_s = (ulid.time_ms(current) - EXTEND_INTERVAL_MS - self.clock.now_ms()) / 1000
            if done.wait(max(0.0, wait_s)):
                return
            next_id = ulid.new(self.clock.now_ms() + ITEM_LEASE_MS)
            status = self._call_until(
                ulid.time_ms(current),
                "q_extend", 3, *layout.extend_keys(item.partition), item.id, current, next_id, item.partition,
            )
            if status is None:
                log.error("error extending lease item=%s", item.id)
                item.lost.set()
                return
            if status != 0:
                log.error("lease extension rejected item=%s status=%r", item.id, status)
                item.lost.set()
                return
            item.lease_id = next_id

    def run_step(self, item: Item, run_id: str) -> bool | None:
        """The step's outcome: True (200), False (500), or None if the lease was lost first.

        Anything but the runner's own answer (a 504 from the load balancer, a reset) says nothing about
        the step, which carries on. Asking again under the same run attaches to it.
        """
        body = json.dumps({"item_id": item.id, "lease_id": run_id, "executor_id": self.id}).encode()
        while not item.lost.is_set():
            request = urllib.request.Request(
                f"{self.runner_url}/run", data=body, headers={"Content-Type": "application/json"}, method="POST"
            )
            try:
                with urllib.request.urlopen(request) as response:
                    if 200 <= response.status < 300:
                        return True
            except urllib.error.HTTPError as exc:
                if exc.code == 500:
                    log.warning("step failed item=%s", item.id)
                    return False
                log.info("no answer for run item=%s run=%s status=%d", item.id, run_id, exc.code)
            except (urllib.error.URLError, OSError) as exc:
                log.info("no answer for run item=%s run=%s err=%r", item.id, run_id, exc)
            item.lost.wait(RUNNER_RETRY_S)
        log.error("lease lost while waiting for run item=%s run=%s", item.id, run_id)
        return None

    def _call_until(self, deadline_ms: int, function: str, numkeys: int, *keys_and_args):
        """Call a lease-conditional function, repeating it while its outcome is unknown.

        Every such function is safe to repeat under the same arguments. Returns None if Valkey
        never answered before deadline_ms.
        """
        while True:
            try:
                return self.valkey.fcall(function, numkeys, *keys_and_args)
            except redis.RedisError as exc:
                if self.clock.now_ms() >= deadline_ms:
                    log.error("%s failed err=%r", function, exc)
                    return None
                time.sleep(RETRY_PAUSE_S)

    def complete(self, item: Item) -> None:
        status = self._call_until(
            ulid.time_ms(item.lease_id),
            "q_dequeue", 3, *layout.dequeue_keys(item.partition), item.id, item.partition, item.lease_id,
        )
        if status not in (0, 1):
            log.error("dequeue failed item=%s status=%r", item.id, status)

    def requeue(self, item: Item, run_at_ms: int) -> None:
        status = self._call_until(
            ulid.time_ms(item.lease_id),
            "q_requeue", 4, *layout.requeue_keys(item.partition), item.id, item.partition, item.lease_id, run_at_ms,
        )
        if status not in (0, 1):
            log.error("requeue failed item=%s status=%r", item.id, status)

    # ── scavenger ──────────────────────────────────────────────────────────────

    def scavenge_loop(self) -> None:
        while not self.stopping.wait(SCAVENGE_INTERVAL_MS / 1000):
            try:
                self.clock.sync()
            except redis.RedisError as exc:
                log.warning("clock sync failed: %r", exc)
            try:
                count = self.scavenge()
            except redis.RedisError as exc:
                log.warning("scavenge failed: %r", exc)
                continue
            if count:
                log.info("scavenged lost items count=%d", count)

    def scavenge(self) -> int:
        """Requeue items whose lease expired, e.g. because their executor died."""
        now = self.clock.now_ms()
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
