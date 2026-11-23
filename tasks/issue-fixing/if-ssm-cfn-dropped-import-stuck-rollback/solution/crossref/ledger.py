"""Reference ledger for exported parameters (one item per SSM parameter in CROSSREF_TABLE).

Item ``#ledger:<parameter name>``: ``{name, exported: BOOL, holders: [readerPhysicalId, ...], rev: N}``.

* ``exported`` — some committed or in-flight producer state still exports the parameter.
* ``holders`` — every reader *physical resource* that imports it.  A reader returns a fresh
  physical id on every Update, so the old id is released only by the engine's cleanup Delete,
  which runs after the consumer update has committed (or after its rollback, for the new id).

A parameter is reclaimed (SSM parameter deleted, then the item) when it is neither exported nor
held.  Both conditions are re-checked in the conditional delete of the item.

Items written by the previous handlers (``{name, tags: {"aws-cdk:strong-ref:<stack>": ...}, rev}``,
keyed by the parameter name) are read, while no ledger item exists, as exported (a parameter only
existed while its writer exported it) and held by ``legacy:<stack>`` per strong-ref tag: the previous
reader kept one physical id for the stack's lifetime, so its hold ends with that id's Delete.  The
first write copies that view into the ledger item, and from then on the legacy item is never read
again: the previous handlers are still the code a stack's functions roll back to, and the previous
reader's Delete (sent for a new reader id in the cleanup after a rolled-back deploy) rewrites the
legacy item and strips the stack's tag.  Our state therefore lives under keys it never touches.
"""
import json
import os

import boto3
from botocore.exceptions import ClientError

TABLE = os.environ.get("CROSSREF_TABLE", "cdk-crossref-tags")
MAX_DELETE_NAMES = 10
LEGACY_TAG_PREFIX = "aws-cdk:strong-ref:"
KEY_PREFIX = "#ledger:"


def _ddb():
    return boto3.client("dynamodb")


def _key(name):
    return {"name": {"S": KEY_PREFIX + name}}


def get(name):
    item = _ddb().get_item(TableName=TABLE, Key=_key(name), ConsistentRead=True).get("Item")
    if not item:
        legacy = _ddb().get_item(TableName=TABLE, Key={"name": {"S": name}}, ConsistentRead=True).get("Item")
        if not legacy:
            return None
        tags = legacy.get("tags", {}).get("M", {})
        return {"exported": True, "holders": sorted(legacy_holder(key.split(":", 2)[2]) for key in tags
                                                    if key.startswith(LEGACY_TAG_PREFIX) and key.count(":") >= 2),
                "rev": None}
    return {
        "exported": item["exported"]["BOOL"],
        "holders": [h["S"] for h in item.get("holders", {}).get("L", [])],
        "rev": int(item["rev"]["N"]),
    }


def legacy_holder(stack):
    return f"legacy:{stack}"


def _put(name, record, prev_rev):
    item = {
        **_key(name),
        "exported": {"BOOL": bool(record["exported"])},
        "holders": {"L": [{"S": h} for h in sorted(set(record["holders"]))]},
        "rev": {"N": str((prev_rev or 0) + 1)},
    }
    if prev_rev is None:
        _ddb().put_item(TableName=TABLE, Item=item, ConditionExpression="attribute_not_exists(#n)",
                        ExpressionAttributeNames={"#n": "name"})
    else:
        _ddb().put_item(TableName=TABLE, Item=item, ConditionExpression="#r = :r",
                        ExpressionAttributeNames={"#r": "rev"}, ExpressionAttributeValues={":r": {"N": str(prev_rev)}})


def modify(name, change, default_exported=True):
    """Apply ``change(record)`` to the item (created if missing) with an optimistic retry loop."""
    for _ in range(20):
        current = get(name)
        prev_rev = current["rev"] if current else None
        record = dict(current) if current else {"exported": default_exported, "holders": []}
        record["holders"] = list(record["holders"])
        change(record)
        try:
            _put(name, record, prev_rev)
            return record
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
    raise RuntimeError(f"ledger contention on {name}")


def reclaim(ssm, names):
    """Delete every parameter in ``names`` that is neither exported nor held any more."""
    doomed = []
    for name in sorted(set(names)):
        record = get(name)
        if record is not None and not record["exported"] and not record["holders"]:
            doomed.append((name, record["rev"]))
    if not doomed:
        return []
    delete_parameters(ssm, [name for name, _ in doomed])
    for name, rev in doomed:
        _ddb().delete_item(TableName=TABLE, Key={"name": {"S": name}})   # the previous handlers' tags
        try:
            _ddb().delete_item(
                TableName=TABLE, Key=_key(name),
                ConditionExpression="#r = :r",
                ExpressionAttributeNames={"#r": "rev"}, ExpressionAttributeValues={":r": {"N": str(rev)}})
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
    return [name for name, _ in doomed]


def generations(event, change):
    """Apply ``change(gens)`` to the record of the reader generations a reader resource has minted.

    ``gens`` maps a reader physical id to ``{"parent": <the physical id it was minted from, until the
    engine shows it adopted it>, "imports": [...]}``.  Item ``#reader:<StackId>/<LogicalResourceId>``.
    """
    key = {"name": {"S": f"#reader:{event['StackId']}/{event['LogicalResourceId']}"}}
    for _ in range(20):
        item = _ddb().get_item(TableName=TABLE, Key=key, ConsistentRead=True).get("Item")
        gens = json.loads(item["gens"]["S"]) if item else {}
        rev = int(item["rev"]["N"]) if item else None
        change(gens)
        new = {**key, "gens": {"S": json.dumps(gens, sort_keys=True)}, "rev": {"N": str((rev or 0) + 1)}}
        try:
            if rev is None:
                _ddb().put_item(TableName=TABLE, Item=new, ConditionExpression="attribute_not_exists(#n)",
                                ExpressionAttributeNames={"#n": "name"})
            else:
                _ddb().put_item(TableName=TABLE, Item=new, ConditionExpression="#r = :r",
                                ExpressionAttributeNames={"#r": "rev"},
                                ExpressionAttributeValues={":r": {"N": str(rev)}})
            return gens
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
    raise RuntimeError("ledger contention on reader generations")


def delete_parameters(ssm, names):
    names = sorted(names)
    for start in range(0, len(names), MAX_DELETE_NAMES):
        chunk = names[start:start + MAX_DELETE_NAMES]
        try:
            ssm.delete_parameters(Names=chunk)
        except ClientError:
            for name in chunk:
                try:
                    ssm.delete_parameter(Name=name)
                except ClientError as exc:
                    if exc.response["Error"]["Code"] != "ParameterNotFound":
                        raise


def current_value(ssm, name):
    try:
        return ssm.get_parameter(Name=name)["Parameter"]["Value"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ParameterNotFound":
            return None
        raise
