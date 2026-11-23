"""Data layer for the ReliabilityQueue table.

One item per submission, keyed by SubmissionID. The forms API writes the item (see
tools/submit.py for the exact shape) before it enqueues the submission; this service only ever
updates it.
"""
import time

from . import config

PENDING = "PENDING"
SENT = "SENT"
DELIVERED = "DELIVERED"
FAILED = "FAILED"


def _key(submission_id):
    return {"SubmissionID": {"S": submission_id}}


def _plain(item):
    """Flatten a DynamoDB attribute map into Python values."""
    out = {}
    for name, value in item.items():
        (kind, raw), = value.items()
        if kind == "N":
            out[name] = int(raw) if raw.lstrip("-").isdigit() else float(raw)
        elif kind == "BOOL":
            out[name] = bool(raw)
        elif kind == "NULL":
            out[name] = None
        else:
            out[name] = raw
    return out


def get_submission(submission_id):
    response = config.dynamodb().get_item(
        TableName=config.TABLE_NAME,
        Key=_key(submission_id),
        ProjectionExpression="SubmissionID, FormID, SendReceipt, FormData, FormSubmissionLanguage, "
        "CreatedAt, SecurityAttribute, NotifyState, NotificationId",
    )
    item = response.get("Item")
    return _plain(item) if item else None


def notify_processed(submission_id, notification_id):
    """Record that Notify accepted the submission's email."""
    config.dynamodb().update_item(
        TableName=config.TABLE_NAME,
        Key=_key(submission_id),
        UpdateExpression="SET NotifyState = :sent, NotificationId = :nid, SentAt = :now",
        ExpressionAttributeValues={
            ":sent": {"S": SENT},
            ":nid": {"S": notification_id},
            ":now": {"N": str(int(time.time()))},
        },
        ReturnValues="NONE",
    )


def remove_processed_mark(submission_id):
    """Clear the processed mark so the submission is picked up again."""
    config.dynamodb().update_item(
        TableName=config.TABLE_NAME,
        Key=_key(submission_id),
        UpdateExpression="SET NotifyState = :pending",
        ExpressionAttributeValues={":pending": {"S": PENDING}},
        ReturnValues="NONE",
    )
