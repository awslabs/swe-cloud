"""Service access for the transaction program.

Every DynamoDB call the program makes funnels through Store.call, which
permits only the single-item operations the protocol is built on:
GetItem, PutItem, UpdateItem, DeleteItem, paginated Scan and table
lifecycle. Multi-item transactional, batch and statement APIs are
rejected here before any request is dispatched.
"""

import os
import time

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

ALLOWED_OPERATIONS = frozenset({
    "get_item",
    "put_item",
    "update_item",
    "delete_item",
    "scan",
    "describe_table",
    "create_table",
    "list_tables",
})


class ContractViolation(RuntimeError):
    """An operation outside the access contract was attempted."""


class ConditionalCheckFailed(Exception):
    """The service rejected a conditional write."""


def now_ms():
    return int(time.time() * 1000)


_SER = TypeSerializer()
_DES = TypeDeserializer()


def _enc(value):
    return _SER.serialize(value)


def _enc_item(item):
    return {name: _enc(value) for name, value in item.items()}


def _dec_item(attr_map):
    if not attr_map:
        return None
    return {name: _DES.deserialize(value) for name, value in attr_map.items()}


class _Tokens(object):
    """Hands out #n / :v placeholders so expressions never need raw names."""

    def __init__(self):
        self.names = {}
        self.values = {}

    def name(self, attr):
        if attr not in self.names.values():
            self.names["#n%d" % len(self.names)] = attr
        for token, mapped in self.names.items():
            if mapped == attr:
                return token

    def value(self, attr, value):
        encoded = _enc(value)
        if encoded not in self.values.values():
            self.values[":v%d" % len(self.values)] = encoded
        for token, mapped in self.values.items():
            if mapped == encoded:
                return token

    def expression_params(self):
        params = {}
        if self.names:
            params["ExpressionAttributeNames"] = self.names
        if self.values:
            params["ExpressionAttributeValues"] = self.values
        return params


def _condition(clauses, tokens):
    """clauses: ("eq", name, value) or ("not_exists", name), joined by AND."""
    parts = []
    for clause in clauses or ():
        if clause[0] == "eq":
            _, name, value = clause
            parts.append("%s = %s" % (tokens.name(name), tokens.value(name, value)))
        elif clause[0] == "not_exists":
            _, name = clause
            parts.append("attribute_not_exists(%s)" % tokens.name(name))
        else:
            raise ValueError("unknown clause %r" % (clause[0],))
    return " AND ".join(parts)


class Store(object):
    def __init__(self, endpoint, region, tx_table, item_table, image_table):
        self.tx_table = tx_table
        self.item_table = item_table
        self.image_table = image_table
        self.client = boto3.client(
            "dynamodb",
            endpoint_url=endpoint or os.environ.get("AWS_ENDPOINT_URL") or "http://localhost:4566",
            region_name=region or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1",
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "test"),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "test"),
        )

    def call(self, operation, **params):
        if operation not in ALLOWED_OPERATIONS:
            raise ContractViolation(
                "operation %r is outside the access contract" % operation)
        try:
            return getattr(self.client, operation)(**params)
        except ClientError as err:
            code = err.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                raise ConditionalCheckFailed(operation)
            raise

    # ---- reads

    def get(self, table, key):
        resp = self.call("get_item", TableName=table, Key=_enc_item(key), ConsistentRead=True)
        return _dec_item(resp.get("Item"))

    def scan(self, table):
        items = []
        start = None
        while True:
            params = {"TableName": table}
            if start:
                params["ExclusiveStartKey"] = start
            resp = self.call("scan", **params)
            for raw in resp.get("Items", []):
                items.append(_dec_item(raw) or {})
            start = resp.get("LastEvaluatedKey")
            if not start:
                return items

    # ---- writes

    def put(self, table, item, clauses=()):
        tokens = _Tokens()
        params = {"TableName": table, "Item": _enc_item(item)}
        condition = _condition(clauses, tokens)
        if condition:
            params["ConditionExpression"] = condition
            params.update(tokens.expression_params())
        try:
            self.call("put_item", **params)
            return True
        except ConditionalCheckFailed:
            return False

    def update(self, table, key, sets=None, removes=None, clauses=()):
        tokens = _Tokens()
        expressions = []
        if sets:
            parts = []
            for name, value in sets.items():
                parts.append("%s = %s" % (tokens.name(name), tokens.value(name, value)))
            expressions.append("SET " + ", ".join(parts))
        if removes:
            expressions.append("REMOVE " + ", ".join(tokens.name(name) for name in removes))
        if not expressions:
            raise ValueError("update with no changes")
        params = {
            "TableName": table,
            "Key": _enc_item(key),
            "UpdateExpression": " ".join(expressions),
        }
        condition = _condition(clauses, tokens)
        if condition:
            params["ConditionExpression"] = condition
        params.update(tokens.expression_params())
        try:
            self.call("update_item", **params)
            return True
        except ConditionalCheckFailed:
            return False

    def delete(self, table, key, clauses=()):
        tokens = _Tokens()
        params = {"TableName": table, "Key": _enc_item(key)}
        condition = _condition(clauses, tokens)
        if condition:
            params["ConditionExpression"] = condition
            params.update(tokens.expression_params())
        try:
            self.call("delete_item", **params)
            return True
        except ConditionalCheckFailed:
            return False

    # ---- table lifecycle

    def ensure_tables(self):
        for table, key_name in (
            (self.tx_table, "txid"),
            (self.item_table, "item_id"),
            (self.image_table, "img"),
        ):
            if self._table_exists(table):
                continue
            self.call(
                "create_table",
                TableName=table,
                KeySchema=[{"AttributeName": key_name, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": key_name, "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
            for _ in range(200):
                if self._table_active(table):
                    break
                time.sleep(0.05)

    def _table_exists(self, table):
        try:
            self.call("describe_table", TableName=table)
            return True
        except ClientError as err:
            if err.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                return False
            raise

    def _table_active(self, table):
        resp = self.call("describe_table", TableName=table)
        return resp["Table"]["TableStatus"] == "ACTIVE"
