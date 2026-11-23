"""Storage helpers for the chunk store: one S3 bucket plus one DynamoDB table.

S3 layout:
  chunks/<opaque-id>                          chunk payloads
  index/<tenant>/<table-id>/<version>.json    immutable index snapshots
  markers/<batch>.json                        deletion markers (audit record)

DynamoDB layout (table keyed by pk/sk, all items of a run share
pk = RUN#<run-id>):
  sk = META    the run request served by the placement
  sk = HEAD    the authoritative index pointer {version, index_key}
  sk = BATCH#<batch>   batch state written by the placement
  sk = OWNER   the placement epoch
  sk = AUDIT#<reader>#<seq>  query-path observations
"""
import json
from decimal import Decimal

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


def s3_client(endpoint, region):
    return boto3.client(
        "s3", endpoint_url=endpoint, region_name=region,
        config=Config(retries={"max_attempts": 4, "mode": "standard"}),
    )


def ddb_resource(endpoint, region):
    return boto3.resource(
        "dynamodb", endpoint_url=endpoint, region_name=region,
        config=Config(retries={"max_attempts": 4, "mode": "standard"}),
    )


def num(value):
    """DynamoDB numbers must be Decimal, never float."""
    return Decimal(str(value)) if isinstance(value, float) else value


def av(value):
    """Type a plain Python value for the client-level attribute-value API."""
    if isinstance(value, bool):
        raise TypeError("bool has no DynamoDB type")
    if isinstance(value, (int, float, Decimal)):
        return {"N": str(value)}
    if isinstance(value, str):
        return {"S": value}
    if isinstance(value, list):
        return {"L": [av(item) for item in value]}
    if isinstance(value, dict):
        return {"M": {k: av(v) for k, v in value.items()}}
    if value is None:
        return {"NULL": True}
    raise TypeError("unsupported value %r" % (value,))


def is_condition_failed(err):
    return isinstance(err, ClientError) and (
        err.response.get("Error", {}).get("Code") in (
            "ConditionalCheckFailedException", "TransactionCanceledException",
            "TransactionInProgressException",
        )
    )


def is_no_such_key(err):
    return isinstance(err, ClientError) and (
        err.response.get("Error", {}).get("Code") in ("NoSuchKey", "404")
    )


class Store:
    """Thin facade over the bucket and the table used by one run."""

    def __init__(self, endpoint, region, bucket, table, run_id):
        self.s3 = s3_client(endpoint, region)
        self.ddb = ddb_resource(endpoint, region)
        # transactions go through a plain client: the resource layer
        # re-types attribute values that are already typed
        self.ddb_client = boto3.client(
            "dynamodb", endpoint_url=endpoint, region_name=region,
            config=Config(retries={"max_attempts": 4, "mode": "standard"}))
        self.bucket = bucket
        self.table = self.ddb.Table(table)
        self.pk = "RUN#%s" % run_id

    # -- S3 ------------------------------------------------------------
    def put_json(self, key, obj):
        self.s3.put_object(Bucket=self.bucket, Key=key,
                           Body=json.dumps(obj).encode())

    def get_json(self, key):
        resp = self.s3.get_object(Bucket=self.bucket, Key=key)
        return json.loads(resp["Body"].read())

    def head(self, key):
        try:
            self.s3.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as exc:
            if is_no_such_key(exc):
                return False
            raise

    def get_bytes(self, key):
        resp = self.s3.get_object(Bucket=self.bucket, Key=key)
        return resp["Body"].read()

    def list_keys(self, prefix):
        out, token = [], None
        while True:
            kw = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kw["ContinuationToken"] = token
            resp = self.s3.list_objects_v2(**kw)
            out += [o["Key"] for o in resp.get("Contents", [])]
            if not resp.get("IsTruncated"):
                return out
            token = resp.get("NextContinuationToken")

    # -- DynamoDB ------------------------------------------------------
    def item(self, sk, consistent=False):
        resp = self.table.get_item(Key={"pk": self.pk, "sk": sk},
                                   ConsistentRead=consistent)
        return resp.get("Item")

    def put_item(self, item, condition=None):
        kw = {"Item": {"pk": self.pk,
                       **{k: num(v) for k, v in item.items()}}}
        if condition:
            kw["ConditionExpression"] = condition
        self.table.put_item(**kw)

    def update_item(self, sk, condition, expression, values, names=None):
        kw = dict(
            Key={"pk": self.pk, "sk": sk},
            ConditionExpression=condition,
            UpdateExpression=expression,
            ExpressionAttributeValues={k: num(v) for k, v in values.items()},
        )
        if names:
            kw["ExpressionAttributeNames"] = names
        self.table.update_item(**kw)

    def transact_write(self, entries):
        """Client-level transaction; plain values are typed here."""
        out = []
        for entry in entries:
            (op, payload), = entry.items()
            p = dict(payload)
            p["TableName"] = self.table.name
            p["Key"] = {k: av(v) for k, v in p["Key"].items()}
            if "ExpressionAttributeValues" in p:
                p["ExpressionAttributeValues"] = {
                    k: av(v) for k, v
                    in p["ExpressionAttributeValues"].items()}
            out.append({op: p})
        self.ddb_client.transact_write_items(TransactItems=out)
