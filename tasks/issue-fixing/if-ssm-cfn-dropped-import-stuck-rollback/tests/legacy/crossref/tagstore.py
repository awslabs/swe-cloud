"""SSM parameter tags, kept in DynamoDB.

The emulator we deploy to does not implement SSM resource tags, so the three tag calls the handlers
make (AddTagsToResource, RemoveTagsFromResource, ListTagsForResource on ResourceType=Parameter) are
provided here with the same semantics: tags belong to an existing parameter, a call naming a
parameter that does not exist raises ``InvalidResourceId``, and deleting a parameter (through
``delete_parameters`` below) deletes its tags with it.

Items in the table (``CROSSREF_TABLE``, partition key ``name``) are ``{name, tags: {key: value},
rev}``; ``rev`` guards the read-modify-write.
"""
import os

import boto3
from botocore.exceptions import ClientError

TABLE = os.environ.get("CROSSREF_TABLE", "cdk-crossref-tags")
MAX_DELETE_NAMES = 10  # DeleteParameters accepts at most 10 names per call


class InvalidResourceId(Exception):
    """The parameter named does not exist."""


def _ddb():
    return boto3.client("dynamodb")


def _require_parameter(ssm, name):
    try:
        ssm.get_parameter(Name=name)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ParameterNotFound":
            raise InvalidResourceId(f"parameter {name} does not exist") from None
        raise


def _load(name):
    item = _ddb().get_item(TableName=TABLE, Key={"name": {"S": name}}, ConsistentRead=True).get("Item")
    if not item:
        return {}, None
    tags = {k: v["S"] for k, v in item.get("tags", {}).get("M", {}).items()}
    return tags, int(item["rev"]["N"])


def _store(name, tags, rev):
    item = {"name": {"S": name}, "tags": {"M": {k: {"S": v} for k, v in tags.items()}},
            "rev": {"N": str((rev or 0) + 1)}}
    if rev is None:
        cond, values = "attribute_not_exists(#n)", {}
    else:
        cond, values = "#r = :rev", {":rev": {"N": str(rev)}}
    kwargs = {"TableName": TABLE, "Item": item, "ConditionExpression": cond,
              "ExpressionAttributeNames": {"#n": "name"} if rev is None else {"#r": "rev"}}
    if values:
        kwargs["ExpressionAttributeValues"] = values
    _ddb().put_item(**kwargs)


def _modify(name, change):
    for _ in range(10):
        tags, rev = _load(name)
        change(tags)
        try:
            _store(name, tags, rev)
            return
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
    raise RuntimeError(f"could not update tags of {name}: too much contention")


def add_tags_to_resource(ssm, name, tags):
    _require_parameter(ssm, name)
    _modify(name, lambda current: current.update(tags))


def remove_tags_from_resource(ssm, name, keys):
    _require_parameter(ssm, name)
    _modify(name, lambda current: [current.pop(k, None) for k in keys])


def list_tags_for_resource(ssm, name):
    _require_parameter(ssm, name)
    tags, _ = _load(name)
    return [{"Key": k, "Value": v} for k, v in sorted(tags.items())]


def delete_parameters(ssm, names):
    """DeleteParameters in chunks of at most 10 names; the tags go with the parameters."""
    names = sorted(names)
    for start in range(0, len(names), MAX_DELETE_NAMES):
        chunk = names[start:start + MAX_DELETE_NAMES]
        if chunk:
            ssm.delete_parameters(Names=chunk)
    for name in names:
        _ddb().delete_item(TableName=TABLE, Key={"name": {"S": name}})
