"""The relay's SQS consumer: poll, hand each message downstream in turn, delete it once handled."""

import collections
import logging
import threading
import time

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from relay.downstream import Downstream
from relay.delivery import Delivery
from relay.extender import VisibilityExtender
from relay.polling import PollingError, Poller
from relay.scheduler import SingleThreadScheduler

LOG = logging.getLogger(__name__)


def make_sqs_client(config):
    return boto3.client(
        "sqs",
        endpoint_url=config.endpoint_url,
        region_name=config.region,
        config=Config(max_pool_connections=20, retries={"mode": "standard", "max_attempts": 3},
                      read_timeout=120),
    )


class Consumer:

    def __init__(self, config, client=None, downstream=None):
        self.config = config
        self.client = client or make_sqs_client(config)
        self.downstream = downstream or Downstream(config.downstream_url, config.relay_id)
        self.visibility_extender = None
        self.scheduler = None
        self.poller = None
        self._stopping = threading.Event()
        self._in_hand = None

    # lifecycle

    def start(self):
        self.poller = Poller(self.client, self.config)
        if self.config.extend_message_visibility and self.scheduler is None:
            visibility_timeout = self.config.visibility_timeout
            if visibility_timeout and visibility_timeout > 0:
                delay = max(1, visibility_timeout // 2)
                self.scheduler = SingleThreadScheduler("SqsVisibilityExtender")
                self.visibility_extender = VisibilityExtender(
                    self.client, self.config.queue_url, visibility_timeout, delay, self._still_handled)
                LOG.debug("scheduled the timeout extender every %s s (visibility timeout %s s)",
                          delay, visibility_timeout)
                self.scheduler.schedule_at_fixed_rate(self.visibility_extender.run, delay, delay)

    def request_stop(self):
        """Asks the relay to stop: the message being handed downstream is finished, nothing else is
        routed, and the loop ends once the poll in progress has returned."""
        self._stopping.set()

    def is_running(self):
        return not self._stopping.is_set()

    def _still_handled(self, delivery_id):
        # once stopping, only the message being handed downstream is still ours to keep hidden
        return self.is_running() or delivery_id == self._in_hand

    def stop(self):
        if self.visibility_extender is not None:
            self.visibility_extender.cancel()
            self.visibility_extender = None
        if self.scheduler is not None:
            self.scheduler.shutdown(wait=True, timeout=30)
            self.scheduler = None
        if self.poller is not None:
            self.poller.close()
            self.poller = None

    def run(self):
        self.start()
        try:
            while not self._stopping.is_set():
                try:
                    handled = self.poll()
                except PollingError as error:
                    LOG.error("%s", error)
                    handled = 0
                if handled == 0 and not self._stopping.is_set():
                    self._stopping.wait(self.config.poll_delay_ms / 1000)
        finally:
            self.stop()

    # polling

    def poll(self):
        # SQS starts the visibility timeout of a message when it returns it, and the poll returns only when
        # its last receive request is done: date the messages from before the requests were sent
        poll_started = time.monotonic()
        messages = self.poller.call()
        deliveries = self.create_deliveries(messages)
        return self.process_batch(deliveries, poll_started)

    def create_deliveries(self, messages):
        LOG.debug("received %d messages in this poll", len(messages))
        return collections.deque(Delivery(message) for message in messages if message)

    def process_batch(self, deliveries, received=None):
        total = len(deliveries)
        if received is None:
            received = time.monotonic()

        # every message of the batch is registered before the first one is routed, so the ones that wait
        # behind a slow message are kept invisible too
        extender = self.visibility_extender
        if extender is not None:
            due = False
            for delivery in deliveries:
                due |= extender.add(delivery, received)
            if due and self.is_running():
                # a message received long before the batch started may expire before the next scheduled run
                self._extend_now(extender)

        index = 0
        while index < total and self.is_running():
            delivery = deliveries.popleft()
            delivery.batch_index = index
            delivery.batch_size = total
            delivery.add_on_completion(self.acknowledge, self.give_back)
            self._in_hand = delivery.delivery_id
            try:
                self.downstream.deliver(delivery)
            except Exception as error:
                delivery.done(failure=error)
            else:
                delivery.done()
            finally:
                self._in_hand = None
            index += 1

        # the messages that are not routed (the relay is stopping) are no longer extended, so they become
        # visible again when their current visibility timeout runs out
        self._forget(extender, deliveries)
        return total

    def _extend_now(self, extender):
        executor = self.scheduler
        if executor is not None:
            try:
                # the executor has a single thread, so this run does not overlap a scheduled one
                executor.execute(extender.run)
            except RuntimeError:
                LOG.debug("cannot extend the received messages now, the relay is stopping")

    @staticmethod
    def _forget(extender, not_routed):
        if extender is not None:
            for delivery in not_routed:
                extender.remove(delivery)

    # completion

    def acknowledge(self, delivery):
        try:
            LOG.debug("deleting message %s", delivery.message_id)
            self.client.delete_message(QueueUrl=self.config.queue_url, ReceiptHandle=delivery.receipt_handle)
        except (BotoCoreError, ClientError) as error:
            LOG.warning("error deleting message %s, ignored: %s", delivery.message_id, error)

    def give_back(self, delivery):
        LOG.warning("error handing message %s downstream, it will be received again: %s",
                    delivery.message_id, delivery.failure)
