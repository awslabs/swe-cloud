"""SQS monitor: receives EventBridge events from the interruption queue (sqs-monitor.go)."""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import SQS_MSG_VISIBILITY_TIMEOUT_SEC_DEFAULT
from .events import SQS_MONITOR_KIND
from .spot_itn import spot_itn_to_interruption_event

log = logging.getLogger("nth.sqs_monitor")

SPOT_ITN_DETAIL_TYPE = "EC2 Spot Instance Interruption Warning"
TEST_NOTIFICATION = "autoscaling:TEST_NOTIFICATION"


class Skip(Exception):
    """Skip processing a message, but acknowledge that something was wrong with it."""


class MonitorError(Exception):
    pass


@dataclass
class NodeInfo:
    name: str
    instance_id: str
    provider_id: str
    is_managed: bool


class SQSMonitor:
    def __init__(self, config, sqs, cluster, interruptions):
        self.config = config
        self.queue_url = config.queue_url
        self.sqs = sqs
        self.cluster = cluster
        self.interruptions = interruptions

    def kind(self):
        return SQS_MONITOR_KIND

    def monitor(self):
        """Receive one batch from the queue and dispose of every message in it."""
        log.debug("checking for queue messages")
        messages = self.receive_queue_messages()

        failed = 0
        for message in messages:
            try:
                event = self.process_sqs_message(message)
            except Skip as skip:
                log.warning("skip processing SQS message: %s", skip)
                continue
            except Exception as err:  # noqa: BLE001 - mirrors the upstream error path
                log.error("error processing SQS message %s: %s", message.get("MessageId"), err)
                failed += 1
                continue

            wrappers = self.process_event_bridge_event(event, message)
            try:
                self.process_interruption_events(wrappers, message)
            except MonitorError as err:
                log.error("error processing interruption events: %s", err)
                failed += 1

        if messages and failed == len(messages):
            raise MonitorError("none of the waiting queue events could be processed")

    def process_sqs_message(self, message):
        event = json.loads(message["Body"])
        if not isinstance(event, dict):
            raise ValueError("message body is not a JSON object")
        if not event.get("detail-type"):
            if TEST_NOTIFICATION in (event.get("Event"), event.get("LifecycleTransition")):
                errors = self.delete_messages([message])
                raise Skip("message is a test notification" + (f" ({errors[0]})" if errors else ""))
            raise ValueError("message is not an EventBridge event")
        return event

    def process_event_bridge_event(self, event, message):
        """Returns a list of (interruption_event, error) pairs for one EventBridge event."""
        source = event.get("source")
        if source == "aws.ec2":
            if event.get("detail-type") == SPOT_ITN_DETAIL_TYPE:
                try:
                    return [(spot_itn_to_interruption_event(self, event, message), None)]
                except Exception as err:  # noqa: BLE001
                    return [(None, err)]
            # State-change and rebalance events are not handled by this port.
            return [(None, None)]
        return [(None, MonitorError(f"event source ({source}) is not supported"))]

    def process_interruption_events(self, wrappers, message):
        drop_message_suggestions = 0
        failed = 0

        for interruption_event, err in wrappers:
            if isinstance(err, Skip):
                log.warning("dropping event: %s", err)
                drop_message_suggestions += 1
            elif err is not None:
                # Log errors and record as failed events. Don't delete the message in order to
                # allow retries.
                log.error("ignoring interruption event due to error: %s", err)
                failed += 1
            elif interruption_event is None:
                log.debug("dropping non-actionable interruption event")
                drop_message_suggestions += 1
            elif self.config.check_tag_before_draining and not interruption_event.is_managed:
                # This event is for an instance that is not managed by this process.
                log.info("dropping interruption event for unmanaged node (instance-id %s)",
                         interruption_event.instance_id)
                drop_message_suggestions += 1
            elif interruption_event.monitor == SQS_MONITOR_KIND:
                log.info("sending %s interruption event to the interruption channel (node %s)",
                         interruption_event.kind, interruption_event.node_name)
                self.interruptions.put(interruption_event)
            else:
                log.warning("dropping interruption event of an unrecognized kind: %s",
                            interruption_event)
                drop_message_suggestions += 1

        if drop_message_suggestions == len(wrappers):
            # All interruption events weren't actionable, just delete the message. If message
            # deletion fails, count it as an error.
            errors = self.delete_messages([message])
            if errors:
                log.error("error deleting message from SQS: %s", errors[0])
                failed += 1

        if failed:
            raise MonitorError(
                f"some interruption events for message Id {message.get('MessageId')} "
                "could not be processed")

    def receive_queue_messages(self):
        visibility_timeout = self.config.sqs_msg_visibility_timeout_sec
        if visibility_timeout <= 0 or visibility_timeout >= 120:
            visibility_timeout = SQS_MSG_VISIBILITY_TIMEOUT_SEC_DEFAULT

        result = self.sqs.receive_message(
            QueueUrl=self.queue_url,
            AttributeNames=["SentTimestamp"],
            MessageAttributeNames=["All"],
            MaxNumberOfMessages=10,
            VisibilityTimeout=visibility_timeout,
            WaitTimeSeconds=20,  # max long polling
        )
        return result.get("Messages", [])

    def delete_messages(self, messages):
        errors = []
        for message in messages:
            try:
                self.sqs.delete_message(QueueUrl=self.queue_url,
                                        ReceiptHandle=message["ReceiptHandle"])
                log.debug("SQS deleted message %s", message.get("MessageId"))
            except Exception as err:  # noqa: BLE001
                errors.append(err)
        return errors

    def get_node_info(self, instance_id):
        """Looks the instance up in the cluster.

        Upstream asks EC2 for the instance and checks the managed tag; here the cluster API is
        the authority: an instance it does not list is not managed by this process.
        """
        node = self.cluster.node_for_instance(instance_id)
        if node is None:
            return NodeInfo(name="", instance_id=instance_id, provider_id="", is_managed=False)
        return NodeInfo(name=node["name"], instance_id=instance_id,
                        provider_id=node.get("providerID", ""), is_managed=True)

    @staticmethod
    def event_time(event):
        raw = event.get("time")
        if raw:
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                pass
        return datetime.now(timezone.utc)
