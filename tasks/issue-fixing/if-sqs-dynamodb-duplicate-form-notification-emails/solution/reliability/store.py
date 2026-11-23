"""Data layer for the ReliabilityQueue table.

One item per submission, keyed by SubmissionID. The forms API writes the item (see
tools/submit.py for the exact shape) before it enqueues the submission; this service only ever
updates it.
"""
import time

from botocore.exceptions import ClientError

from . import config

PENDING = "PENDING"
SENT = "SENT"
DELIVERED = "DELIVERED"
FAILED = "FAILED"
CLAIMED = "CLAIMED"
DONE = (SENT, DELIVERED, FAILED)


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


def _conditional(**kwargs):
    """Run a conditional UpdateItem; False when the condition did not hold."""
    try:
        config.dynamodb().update_item(TableName=config.TABLE_NAME, ReturnValues="NONE", **kwargs)
        return True
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


def get_submission(submission_id):
    response = config.dynamodb().get_item(
        TableName=config.TABLE_NAME, Key=_key(submission_id), ConsistentRead=True
    )
    item = response.get("Item")
    return _plain(item) if item else None


def attempt_of(item):
    return int(item.get("Attempt", 1))


def _attempt_condition(item, values):
    if "Attempt" in item:
        values[":attempt"] = {"N": str(attempt_of(item))}
        return "Attempt = :attempt"
    return "attribute_not_exists(Attempt)"


def claim(submission_id, item, owner, lease_until):
    """Take the submission for its current attempt, from PENDING or from an expired claim."""
    values = {
        ":claimed": {"S": CLAIMED},
        ":owner": {"S": owner},
        ":lease": {"N": str(lease_until)},
        ":set_attempt": {"N": str(attempt_of(item))},
    }
    condition = [_attempt_condition(item, values)]
    state = item.get("NotifyState", PENDING)
    values[":seen_state"] = {"S": state}
    condition.append("NotifyState = :seen_state")
    if state == CLAIMED:
        values[":seen_owner"] = {"S": item.get("ClaimOwner", "")}
        values[":seen_lease"] = {"N": str(item.get("LeaseExpiresAt", 0))}
        condition.append("ClaimOwner = :seen_owner AND LeaseExpiresAt = :seen_lease")
    return _conditional(
        Key=_key(submission_id),
        UpdateExpression="SET NotifyState = :claimed, ClaimOwner = :owner, "
        "LeaseExpiresAt = :lease, Attempt = :set_attempt",
        ConditionExpression=" AND ".join(condition),
        ExpressionAttributeValues=values,
    )


def mark_sent(submission_id, attempt, owner, notification_id):
    """Record the accepted notification, only while we still hold the claim for this attempt."""
    return _conditional(
        Key=_key(submission_id),
        UpdateExpression="SET NotifyState = :sent, NotificationId = :nid, SentAt = :now "
        "REMOVE ClaimOwner, LeaseExpiresAt",
        ConditionExpression="Attempt = :attempt AND NotifyState = :claimed AND ClaimOwner = :owner",
        ExpressionAttributeValues={
            ":sent": {"S": SENT},
            ":nid": {"S": notification_id},
            ":now": {"N": str(int(time.time()))},
            ":attempt": {"N": str(attempt)},
            ":claimed": {"S": CLAIMED},
            ":owner": {"S": owner},
        },
    )


def revoke(submission_id, notification_id):
    """Reopen the submission for one more attempt if `notification_id` is the one that stands."""
    item = get_submission(submission_id)
    if not item or item.get("NotificationId") != notification_id:
        return False
    if item.get("NotifyState") not in (SENT, DELIVERED):
        return False
    attempt = attempt_of(item)
    values = {
        ":pending": {"S": PENDING},
        ":next": {"N": str(attempt + 1)},
        ":nid": {"S": notification_id},
        ":sent": {"S": SENT},
        ":delivered": {"S": DELIVERED},
    }
    condition = "NotificationId = :nid AND (NotifyState = :sent OR NotifyState = :delivered) AND " + _attempt_condition(
        item, values
    )
    return _conditional(
        Key=_key(submission_id),
        UpdateExpression="SET NotifyState = :pending, Attempt = :next, RevokedNotificationId = :nid "
        "REMOVE NotificationId",
        ConditionExpression=condition,
        ExpressionAttributeValues=values,
    )


def settle(submission_id, notification_id, state):
    """DELIVERED / FAILED for the notification that stands; ignored for any other."""
    return _conditional(
        Key=_key(submission_id),
        UpdateExpression="SET NotifyState = :state",
        ConditionExpression="NotificationId = :nid AND (NotifyState = :sent OR NotifyState = :delivered)",
        ExpressionAttributeValues={
            ":state": {"S": state},
            ":nid": {"S": notification_id},
            ":sent": {"S": SENT},
            ":delivered": {"S": DELIVERED},
        },
    )


def _untouched():
    return "attribute_not_exists(Attempt) AND NotifyState = :pending"


def adopt_previous(submission_id, notification_id, state):
    """Record the previous version's standing email, if this version has not started on the item."""
    return _conditional(
        Key=_key(submission_id),
        UpdateExpression="SET NotifyState = :state, NotificationId = :nid, Attempt = :one",
        ConditionExpression=_untouched(),
        ExpressionAttributeValues={
            ":state": {"S": state},
            ":nid": {"S": notification_id},
            ":one": {"N": "1"},
            ":pending": {"S": PENDING},
        },
    )


def reopen_previous(submission_id, notification_id):
    """The previous version's standing email failed: open attempt 2 for its one replacement."""
    return _conditional(
        Key=_key(submission_id),
        UpdateExpression="SET Attempt = :two, RevokedNotificationId = :nid REMOVE NotificationId",
        ConditionExpression=_untouched(),
        ExpressionAttributeValues={
            ":two": {"N": "2"},
            ":nid": {"S": notification_id},
            ":pending": {"S": PENDING},
        },
    )
