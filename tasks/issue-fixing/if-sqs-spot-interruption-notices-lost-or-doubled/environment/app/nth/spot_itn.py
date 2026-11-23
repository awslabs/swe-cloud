"""EC2 Spot Instance Interruption Warning -> interruption event (spot-itn-event.go).

Example event as EventBridge delivers it to the queue:

    {
      "version": "0",
      "id": "1e5527d7-bb36-4607-3370-4164db56a40e",
      "detail-type": "EC2 Spot Instance Interruption Warning",
      "source": "aws.ec2",
      "account": "123456789012",
      "time": "2024-01-01T00:00:00Z",
      "region": "us-east-1",
      "resources": ["arn:aws:ec2:us-east-1b:instance/i-0b662ef9931388ba0"],
      "detail": {"instance-id": "i-0b662ef9931388ba0", "instance-action": "terminate"}
    }
"""

import logging

from .events import SPOT_ITN_KIND, SQS_MONITOR_KIND, InterruptionEvent

log = logging.getLogger("nth.spot_itn")


def spot_itn_to_interruption_event(monitor, event, message):
    detail = event.get("detail") or {}
    instance_id = detail.get("instance-id", "")
    if not instance_id:
        raise ValueError("spot interruption event has no instance-id")

    node_info = monitor.get_node_info(instance_id)

    interruption_event = InterruptionEvent(
        event_id=f"spot-itn-event-{event.get('id', '').encode().hex()}",
        kind=SPOT_ITN_KIND,
        monitor=SQS_MONITOR_KIND,
        start_time=monitor.event_time(event),
        instance_id=instance_id,
        node_name=node_info.name,
        provider_id=node_info.provider_id,
        is_managed=node_info.is_managed,
        description=(f"Spot Interruption notice for instance {instance_id} "
                     f"was sent at {event.get('time', '')}"),
    )

    def post_drain_task(_event):
        errors = monitor.delete_messages([message])
        if errors:
            raise errors[0]

    interruption_event.post_drain_task = post_drain_task
    return interruption_event
