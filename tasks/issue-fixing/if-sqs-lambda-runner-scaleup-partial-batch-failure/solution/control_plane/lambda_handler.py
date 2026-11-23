"""Lambda entry points of the control plane.

Port of lambdas/functions/control-plane/src/lambda.ts (scale-up handler).
"""

from __future__ import annotations

import json
from typing import Any

from .logger import create_child_logger
from .scale_up import CONFIGURED, DROPPED, IGNORED, scale_up

logger = create_child_logger("lambda")


def scale_up_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    sqs_messages: list[dict[str, Any]] = []
    warned_event_sources: set[str] = set()

    for record in event.get("Records", []):
        event_source = record.get("eventSource")
        message_id = record.get("messageId")
        if event_source != "aws:sqs":
            if event_source not in warned_event_sources:
                logger.warn("Ignoring non-sqs event source", eventSource=event_source)
                warned_event_sources.add(event_source)
            continue

        try:
            payload = json.loads(record["body"])
        except (ValueError, TypeError, KeyError) as error:
            # A malformed body is a permanent, non-retryable failure. Keep it out of
            # batchItemFailures so the event source mapping acknowledges and deletes it,
            # while valid records in the same batch continue to scale up normally.
            logger.error(f"Ignoring message {message_id}, body is not valid JSON", error=str(error), messageId=message_id)
            continue
        sqs_messages.append({**payload, "messageId": message_id})

    # Sort messages by their retry count, so that we retry the same messages if there's a
    # persistent failure. This should cause messages to be dropped quicker than if we retried in
    # an arbitrary order.
    sqs_messages.sort(key=lambda message: message.get("retryCounter") or 0)

    # The SQS event source mapping owns message acknowledgement and deletion. Because
    # ReportBatchItemFailures is enabled, a successful handler response makes Lambda delete every
    # record not listed here; listed records remain in SQS and become available for retry after
    # their visibility timeout expires.
    batch_item_failures: list[dict[str, str]] = []
    outcomes: dict[str, str] = {}
    remaining_ms = getattr(context, "get_remaining_time_in_millis", None) or (lambda: 10**9)

    try:
        retry_message_ids = scale_up(sqs_messages, outcomes, remaining_ms)

        if retry_message_ids:
            logger.warn("SQS messages will be retried.", messageIds=retry_message_ids)

        for message_id in retry_message_ids:
            batch_item_failures.append({"itemIdentifier": message_id})

        return {"batchItemFailures": batch_item_failures}
    except Exception as error:  # noqa: BLE001
        # Records that already have a runner (or were deliberately ignored) must not come back:
        # a redelivery would launch a second runner for them. Everything else is retried.
        settled = (CONFIGURED, IGNORED, DROPPED)
        retry = [message["messageId"] for message in sqs_messages if outcomes.get(message["messageId"]) not in settled]
        logger.error(
            f"Error processing batch (size: {len(sqs_messages)}): {error}, retrying {len(retry)} undecided message(s)",
            error=repr(error),
        )
        return {"batchItemFailures": [{"itemIdentifier": message_id} for message_id in retry]}
