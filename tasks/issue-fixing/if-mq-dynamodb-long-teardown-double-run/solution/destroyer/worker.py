"""Destroyer worker: one long-running process per host.

    python3 -m destroyer.worker --worker-id W1

Consumes destroy requests ({"vm_id": "..."}) from the work queue and tears each VM down with
vm-destroy.

vm.destroy enforces a delivery acknowledgement timeout far shorter than a big teardown, so no
delivery is held for the length of a job. Ownership is a lease on the fleet record, renewed only
when vm-destroy reports progress. While a VM is being destroyed exactly one check-back message for it
is parked on ``destroyer.checkback``, which dead-letters back to the work queue after a short TTL;
each time it comes back, whichever worker receives it either finds the VM destroyed (drop it), finds
a live lease (park it again), or finds the lease expired and takes the VM over. The check-back is
confirmed by the broker before the delivery that caused it is acked, so a VM is never only in the
memory of a worker that might die.
"""

import argparse
import json
import logging
import signal
import sys
import time

import pika

from destroyer.config import from_env
from destroyer.fleet import Fleet, now_ms
from destroyer.teardown import run_destroy

log = logging.getLogger("destroyer.worker")

LEASE_S = 15
CHECKBACK_QUEUE = "destroyer.checkback"
CHECKBACK_TTL_MS = 10000
AMQP_ERRORS = (pika.exceptions.AMQPError, OSError)


class Worker:
    def __init__(self, settings):
        self.settings = settings
        self.fleet = Fleet(settings.fleet_table)
        self.connection = None
        self.channel = None
        self.consumer_tag = None
        self.job = None

    # ── broker ────────────────────────────────────────────────────────────────

    def connect(self):
        params = pika.URLParameters(self.settings.amqp_url)
        params.heartbeat = 30
        self.connection = pika.BlockingConnection(params)
        self.channel = self.connection.channel()
        self.channel.confirm_delivery()
        self.channel.basic_qos(prefetch_count=1)
        self.channel.queue_declare(CHECKBACK_QUEUE, durable=True, arguments={
            "x-message-ttl": CHECKBACK_TTL_MS,
            "x-dead-letter-exchange": "",
            "x-dead-letter-routing-key": self.settings.work_queue,
        })
        self.consumer_tag = None

    def disconnect(self):
        try:
            if self.connection is not None and self.connection.is_open:
                self.connection.close()
        except AMQP_ERRORS:
            pass
        self.connection = self.channel = self.consumer_tag = None

    def subscribe(self):
        if self.consumer_tag is None:
            self.consumer_tag = self.channel.basic_consume(self.settings.work_queue, self.on_request)
            log.info("%s consuming from %s", self.settings.worker_id, self.settings.work_queue)

    def unsubscribe(self):
        if self.consumer_tag is not None:
            self.channel.basic_cancel(self.consumer_tag)
            self.consumer_tag = None

    def park(self, vm_id):
        """Publish the VM's check-back; returns once the broker has confirmed it."""
        self.channel.basic_publish(
            "", CHECKBACK_QUEUE, json.dumps({"vm_id": vm_id}).encode(),
            pika.BasicProperties(delivery_mode=2, content_type="application/json"),
        )

    # ── decisions ─────────────────────────────────────────────────────────────

    def on_request(self, channel, method, properties, body):
        vm_id = json.loads(body)["vm_id"]
        record = self.fleet.get(vm_id)
        if record is None:
            log.warning("%s: not in the fleet table, dropping request", vm_id)
            channel.basic_ack(method.delivery_tag)
            return
        if record.get("state") == "destroyed":
            channel.basic_ack(method.delivery_tag)
            return
        if record.get("state") == "destroying" and int(record.get("lease_until", 0)) > now_ms():
            self.park(vm_id)
            channel.basic_ack(method.delivery_tag)
            return
        epoch = int(record.get("epoch", 0))
        if not self.fleet.claim(vm_id, self.settings.worker_id, epoch, LEASE_S * 1000):
            self.park(vm_id)
            channel.basic_ack(method.delivery_tag)
            return
        self.park(vm_id)
        self.unsubscribe()
        channel.basic_ack(method.delivery_tag)
        self.job = (vm_id, epoch + 1)
        log.info("%s: claimed at epoch %s (previous owner %s)", vm_id, epoch + 1, record.get("destroyer"))

    def destroy(self, vm_id, epoch):
        heard = time.monotonic()
        owned = True

        def on_progress(_n):
            nonlocal heard, owned
            heard = time.monotonic()
            if not self.fleet.renew(vm_id, self.settings.worker_id, epoch, LEASE_S * 1000):
                owned = False

        def idle():
            if self.connection is not None:
                try:
                    self.connection.process_data_events(0)
                except AMQP_ERRORS as error:
                    log.warning("broker connection lost during %s: %r", vm_id, error)
                    self.disconnect()
            return owned and time.monotonic() - heard < LEASE_S

        status = run_destroy(vm_id, on_progress=on_progress, idle=idle)
        if status == 0:
            if self.fleet.mark_destroyed(vm_id, self.settings.worker_id, epoch):
                log.info("%s: destroyed", vm_id)
            else:
                log.error("%s: destroyed, but epoch %s no longer owns the record", vm_id, epoch)
        else:
            log.warning("%s: vm-destroy exited %s; leaving the VM to its check-back", vm_id, status)

    # ── loop ──────────────────────────────────────────────────────────────────

    def run(self):
        while True:
            try:
                if self.connection is None:
                    self.connect()
                if self.job is None:
                    self.subscribe()
                self.connection.process_data_events(1)
            except AMQP_ERRORS as error:
                log.warning("%s: broker connection lost: %r", self.settings.worker_id, error)
                self.disconnect()
                time.sleep(1)
            if self.job is not None:
                vm_id, epoch = self.job
                try:
                    self.destroy(vm_id, epoch)
                finally:
                    self.job = None


def main(argv=None):
    parser = argparse.ArgumentParser(prog="destroyer.worker")
    parser.add_argument("--worker-id", required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    Worker(from_env(args.worker_id)).run()


if __name__ == "__main__":
    main()
