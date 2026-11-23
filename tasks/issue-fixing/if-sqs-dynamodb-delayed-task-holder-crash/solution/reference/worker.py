"""Delayed-task runner.

Same process, same protocol and same row accounting as before. What changed
is how a held delivery relates to the queue and to the task row:

* A delivery is hidden for a short window that this holder keeps extending
  while it still holds the task, so a window that ends — because the holder
  died, or stopped, or lost the task — is followed promptly by the queue
  offering the work to someone else.
* Taking a delivery means claiming the task on its row, and the claim is the
  only thing that entitles a holder to commit. A second copy of a delivery
  and a callback left over in a holder that has since lost the task both
  find the row closed against them.
"""

import argparse
import json
import os
import select
import sys
import time

import broker
import settings
import store
from timer import Schedule

HOLD_S = 4.0        # hiding window granted per extension
RENEW_EVERY = 1.0   # how often a holder extends window and lease
FIRE_SLACK = 0.15   # fire just past due so the commit's due test holds


class Runner:
    def __init__(self, worker_id, queue_url, table, run_id, endpoint, region):
        self.worker_id = worker_id
        self.run_id = run_id
        self.queue = broker.Queue(queue_url, endpoint, region)
        self.table = store.Table(table, endpoint, region)
        self.schedule = Schedule()
        self.polling = False
        self.in_buffer = b""
        self.held = {}
        self.last_renew = 0.0

    def emit(self, event, **fields):
        line = {"event": event, "worker_id": self.worker_id, "at": time.time()}
        line.update(fields)
        sys.stdout.write(json.dumps(line) + "\n")
        sys.stdout.flush()

    # ---- taking work --------------------------------------------------

    def take_delivery(self, delivery):
        body = broker.decode(delivery)
        task_id = body["task_id"]
        handle = delivery["ReceiptHandle"]
        try:
            self.queue.hide_for(handle, HOLD_S)
        except Exception:
            pass

        row = self.table.get_task(self.run_id, task_id)
        if row and row["phase"] == store.DONE:
            self.clear_copy(handle)
            self.emit("CLEARED", task_id=task_id, message_id=delivery["MessageId"])
            return

        epoch = self.table.claim(
            self.run_id, task_id, delivery["MessageId"], body["due_at"], self.worker_id
        )
        if epoch is None:
            # Taken and reported, but the task stays with its holder; this
            # copy is left to lapse back to the queue.
            self.emit(
                "HELD",
                task_id=task_id,
                message_id=delivery["MessageId"],
                receipt=broker.digest(handle),
            )
            self.emit("DECLINED", task_id=task_id, message_id=delivery["MessageId"])
            return

        entry = {
            "task_id": task_id,
            "handle": handle,
            "message_id": delivery["MessageId"],
            "due_at": body["due_at"],
            "epoch": epoch,
        }
        self.held[(task_id, broker.digest(handle))] = entry
        self.schedule.call_at(body["due_at"] + FIRE_SLACK, self.apply_eta_task, entry)
        self.emit(
            "HELD",
            task_id=task_id,
            message_id=delivery["MessageId"],
            receipt=broker.digest(handle),
        )

    def clear_copy(self, handle):
        """Remove a delivery we are done with. A copy the queue has already
        handed to someone else may not be removable through this handle, and
        that is not worth stopping for."""
        try:
            self.queue.remove(handle)
        except Exception:
            pass

    def apply_eta_task(self, entry):
        task_id = entry["task_id"]
        self.held.pop((task_id, broker.digest(entry["handle"])), None)
        done_at = self.table.commit_effect(
            self.run_id, task_id, self.worker_id, entry["epoch"]
        )
        if done_at is not None:
            self.clear_copy(entry["handle"])
            self.emit("FIRED", task_id=task_id)
            return
        row = self.table.get_task(self.run_id, task_id)
        if row and row["phase"] == store.DONE:
            self.clear_copy(entry["handle"])
            self.emit("CLEARED", task_id=task_id)
        else:
            self.emit("DECLINED", task_id=task_id)

    # ---- keeping what is ours -----------------------------------------

    def keep_held(self):
        now = time.time()
        if now - self.last_renew < RENEW_EVERY:
            return
        self.last_renew = now
        for key, entry in list(self.held.items()):
            task_id = entry["task_id"]
            ours = self.table.renew(
                self.run_id, task_id, self.worker_id, entry["epoch"]
            )
            if not ours:
                row = self.table.get_task(self.run_id, task_id)
                if row and row["phase"] == store.DONE:
                    self.held.pop(key, None)
                    continue
                if row and row["holder"] == self.worker_id:
                    continue          # transient failure, keep the entry
                self.held.pop(key, None)
                self.schedule.cancel(entry)
                self.emit("RELEASED", task_id=task_id)
                continue
            try:
                self.queue.hide_for(entry["handle"], HOLD_S)
            except Exception:
                pass              # the lease still holds; retry next pass

    # ---- commands -----------------------------------------------------

    def cmd_receive(self):
        deliveries = self.queue.receive(count=1, wait=0)
        if not deliveries:
            self.emit("IDLE")
            return
        for delivery in deliveries:
            self.take_delivery(delivery)

    def handle_line(self, line):
        command = json.loads(line)
        kind = command.get("cmd")
        if kind == "RECEIVE":
            self.cmd_receive()
        elif kind == "ADOPT":
            self.take_delivery(command["message"])
        elif kind == "POLL":
            self.polling = bool(command.get("enable", True))
        elif kind == "STOP":
            raise SystemExit(0)

    def read_commands(self):
        while True:
            ready, _, _ = select.select([sys.stdin], [], [], 0)
            if not ready:
                return
            chunk = os.read(sys.stdin.fileno(), 65536)
            if not chunk:
                return
            self.in_buffer += chunk
            while b"\n" in self.in_buffer:
                line, self.in_buffer = self.in_buffer.split(b"\n", 1)
                if line.strip():
                    self.handle_line(line.decode("utf-8"))

    # ---- main loop ----------------------------------------------------

    def dispatch(self, callback, entry):
        """Run one due entry, retrying shortly if a service call fails."""
        try:
            callback(entry)
        except Exception:
            self.schedule.call_at(time.time() + 0.5, callback, entry)

    def run(self):
        self.emit("READY")
        while True:
            self.read_commands()
            if self.polling:
                try:
                    for delivery in self.queue.receive(count=1, wait=0):
                        self.take_delivery(delivery)
                except Exception:
                    pass
            self.keep_held()
            for callback, delivery in self.schedule.due():
                self.dispatch(callback, delivery)
            time.sleep(settings.POLL_INTERVAL_S)


def main():
    parser = argparse.ArgumentParser(description="Delayed-task runner.")
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--queue-url", required=True)
    parser.add_argument("--table", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--endpoint", default=os.environ.get("AWS_ENDPOINT_URL"))
    parser.add_argument(
        "--region", default=os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    )
    args = parser.parse_args()
    runner = Runner(
        args.worker_id,
        args.queue_url,
        args.table,
        args.run_id,
        args.endpoint,
        args.region,
    )
    runner.run()


if __name__ == "__main__":
    main()
