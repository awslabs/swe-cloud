import os

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.config import Config

ENTRIES = "seal_entries"
META = "seal_meta"
_serializer = TypeSerializer()
_deserializer = TypeDeserializer()


def encode(item):
    return {k: _serializer.serialize(v) for k, v in item.items()}


def decode(item):
    result = {k: _deserializer.deserialize(v) for k, v in item.items()}
    return {k: bytes(v) if hasattr(v, "value") and isinstance(v.value, bytes) else v
            for k, v in result.items()}


class Store:
    def __init__(self):
        kwargs = dict(endpoint_url=os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566"),
                      region_name="us-east-1", aws_access_key_id="test",
                      aws_secret_access_key="test",
                      config=Config(connect_timeout=3, read_timeout=90,
                                    retries={"max_attempts": 2, "mode": "standard"}))
        self.ddb = boto3.client("dynamodb", **kwargs)
        self.kms = boto3.client("kms", **kwargs)

    def read(self, table, key):
        response = self.ddb.get_item(TableName=table, Key=encode(key), ConsistentRead=True)
        return decode(response["Item"]) if "Item" in response else None

    def config(self):
        value = self.read(META, {"pk": "config"})
        if not value:
            raise RuntimeError("seal is not initialized; see README.md")
        return value

    def entries(self):
        args = dict(TableName=ENTRIES, ConsistentRead=True)
        while True:
            page = self.ddb.scan(**args)
            yield from (decode(item) for item in page.get("Items", []))
            if not page.get("LastEvaluatedKey"):
                return
            args["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def put(self, table, item, **kwargs):
        self.ddb.put_item(TableName=table, Item=encode(item), **kwargs)
