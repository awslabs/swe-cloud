"""Delayed-task runner.

One process per worker. A worker takes a delivery from the queue as soon as
the queue offers it, schedules the task's callback on its local schedule for
the task's due time, and holds the delivery until then. When the callback
runs it commits the task's effect and removes the delivery from the queue.

Commands arrive as JSON lines on stdin; events go out as JSON lines on
stdout. See README.md for the protocol.
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


class Runner:
    def __init__(self, worker_id, queue_url, table, run_id, endpoint, region):
        self.worker_id = worker_id
        self.run_id = run_id
        self.queue = broker.Queue(queue_url, endpoint, region)
        self.table = store.Table(table, endpoint, region)
        self.schedule = Schedule()
        self.polling = False
        self.in_buffer = b""

    # ---- output -------------------------------------------------------

    def emit(self, event, **fields):
        line = {"event": event, "worker_id": self.worker_id, "at": time.time()}
        line.update(fields)
        sys.stdout.write(json.dumps(line) + "\n")
        sys.stdout.flush()

    # ---- taking work --------------------------------------------------

    def take_delivery(self, delivery):
        """Hold one delivery until its task is due."""
        body = broker.decode(delivery)
        handle = delivery["ReceiptHandle"]
        self.queue.hide_for(handle, settings.VISIBILITY_S)
        self.table.record_task(
            self.run_id,
            body["task_id"],
            delivery["MessageId"],
            body["due_at"],
            self.worker_id,
        )
        self.schedule.call_at(body["due_at"], self.apply_eta_task, delivery)
        self.emit(
            "HELD",
            task_id=body["task_id"],
            message_id=delivery["MessageId"],
            receipt=broker.digest(handle),
        )

    def apply_eta_task(self, delivery):
        """Run one held task: commit its effect, then clear the delivery."""
        body = broker.decode(delivery)
        self.table.commit_effect(self.run_id, body["task_id"], self.worker_id)
        self.queue.remove(delivery["ReceiptHandle"])
        self.emit("FIRED", task_id=body["task_id"])

    # ---- commands -----------------------------------------------------

    def cmd_receive(self):
        deliveries = self.queue.receive(count=1, wait=0)
        if not deliveries:
            self.emit("IDLE")
            return
        for delivery in deliveries:
            self.take_delivery(delivery)

    def cmd_adopt(self, message):
        self.take_delivery(message)

    def cmd_poll(self, enable):
        self.polling = bool(enable)

    def handle_line(self, line):
        command = json.loads(line)
        kind = command.get("cmd")
        if kind == "RECEIVE":
            self.cmd_receive()
        elif kind == "ADOPT":
            self.cmd_adopt(command["message"])
        elif kind == "POLL":
            self.cmd_poll(command.get("enable", True))
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

    def run(self):
        self.emit("READY")
        while True:
            self.read_commands()
            if self.polling:
                for delivery in self.queue.receive(count=1, wait=0):
                    self.take_delivery(delivery)
            for callback, delivery in self.schedule.due():
                callback(delivery)
            time.sleep(settings.POLL_INTERVAL_S)


def main():
    parser = argparse.ArgumentParser(description="Delayed-task runner.")
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--queue-url", required=True)
    parser.add_argument("--table", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--endpoint", default=os.environ.get("AWS_ENDPOINT_URL"))
    parser.add_argument("--region", default=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    args = parser.parse_args()
    runner = Runner(
        args.worker_id, args.queue_url, args.table, args.run_id, args.endpoint, args.region
    )
    runner.run()


if __name__ == "__main__":
    main()
