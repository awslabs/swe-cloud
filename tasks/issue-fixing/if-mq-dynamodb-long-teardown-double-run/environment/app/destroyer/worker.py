"""Destroyer worker: one long-running process per host.

    python3 -m destroyer.worker --worker-id W1

Consumes destroy requests ({"vm_id": "..."}) from the work queue, tears each VM down with
vm-destroy, records the result in the fleet table and acknowledges the request.
"""

import argparse
import json
import logging
import signal
import sys

import pika

from destroyer.config import from_env
from destroyer.fleet import Fleet
from destroyer.teardown import run_destroy

log = logging.getLogger("destroyer.worker")


class Worker:
    def __init__(self, settings):
        self.settings = settings
        self.fleet = Fleet(settings.fleet_table)
        self.connection = None
        self.channel = None

    def connect(self):
        self.connection = pika.BlockingConnection(pika.URLParameters(self.settings.amqp_url))
        self.channel = self.connection.channel()
        self.channel.basic_qos(prefetch_count=self.settings.prefetch)
        self.channel.basic_consume(self.settings.work_queue, self.on_request)
        log.info("%s consuming from %s", self.settings.worker_id, self.settings.work_queue)

    def on_request(self, channel, method, properties, body):
        vm_id = json.loads(body)["vm_id"]
        record = self.fleet.get(vm_id)
        if record is None:
            log.warning("%s: not in the fleet table, dropping request", vm_id)
            channel.basic_ack(method.delivery_tag)
            return
        if record.get("state") == "destroyed":
            log.info("%s: already destroyed", vm_id)
            channel.basic_ack(method.delivery_tag)
            return

        self.fleet.mark_destroying(vm_id, self.settings.worker_id)
        log.info("%s: destroying (redelivered=%s)", vm_id, method.redelivered)
        status = run_destroy(vm_id, idle=lambda: self.connection.process_data_events(0))
        if status == 0:
            self.fleet.mark_destroyed(vm_id, self.settings.worker_id)
            channel.basic_ack(method.delivery_tag)
            log.info("%s: destroyed", vm_id)
        else:
            log.warning("%s: vm-destroy exited %s, returning the request to the queue", vm_id, status)
            channel.basic_nack(method.delivery_tag, requeue=True)

    def run(self):
        self.connect()
        try:
            self.channel.start_consuming()
        except pika.exceptions.ChannelClosedByBroker as closed:
            log.error("%s: broker closed the channel: %s", self.settings.worker_id, closed)
            raise SystemExit(1)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="destroyer.worker")
    parser.add_argument("--worker-id", required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    Worker(from_env(args.worker_id)).run()


if __name__ == "__main__":
    main()
