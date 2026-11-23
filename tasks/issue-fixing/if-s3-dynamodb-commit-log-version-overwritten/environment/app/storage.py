"""Storage channel for the table log store.

One object owns every call the store makes to object storage and to the
coordination table: S3 for log bytes and temporary action files, DynamoDB for
per-version coordination entries. Store code must talk to the services through
this module and not through the AWS SDKs directly -- this is the supported
surface, and deployments instrument it (every operation is announced on the
operations socket named by SEAM_SOCKET when one is configured, and the process
waits there until the operator replies).

Configuration comes from the environment:

  BUCKET       object-storage bucket holding the tables      (default logstore-dev)
  LOG_TABLE    DynamoDB table holding coordination entries   (default log_coord)
  TABLE_PATH   prefix of the table this process works on     (default tables/demo)
"""

import json
import os
import re
import socket
import time

import boto3
from botocore.config import Config

try:
    from botocore.exceptions import ClientError
except ImportError:  # pragma: no cover - boto3 always brings botocore
    ClientError = Exception


class EntryExists(Exception):
    """A coordination entry for this version is already present."""


class EntryGone(Exception):
    """The coordination entry this process recorded is no longer its own."""


_FINAL_KEY_RE = re.compile(r"/log/\d{20}\.json$")


def _announce(event, **facts):
    """Report a point in the operation flow and wait for the operator's reply.

    With no SEAM_SOCKET configured this returns immediately; an unreachable or
    silent operator never breaks the store -- the wait is bounded.
    """
    path = os.environ.get("SEAM_SOCKET")
    if not path:
        return
    payload = {"event": event, "pid": os.getpid()}
    payload.update({k: v for k, v in facts.items() if v is not None})
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(60.0)
            client.connect(path)
            client.sendall((json.dumps(payload) + "\n").encode())
            try:
                client.recv(64)
            except socket.timeout:
                pass
    except OSError:
        pass


class Storage:
    def __init__(self, bucket, log_table, table_path, endpoint=None, region=None):
        self.bucket = bucket
        self.log_table = log_table
        self.table_path = table_path
        endpoint = endpoint or os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566")
        region = region or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        config = Config(retries={"max_attempts": 2}, connect_timeout=5, read_timeout=15)
        self.s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name=region,
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "test"),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "test"),
            config=config,
        )
        self.ddb = boto3.client(
            "dynamodb",
            endpoint_url=endpoint,
            region_name=region,
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "test"),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "test"),
            config=config,
        )
        self._ensure_resources()

    @classmethod
    def from_env(cls):
        return cls(
            bucket=os.environ.get("BUCKET", "logstore-dev"),
            log_table=os.environ.get("LOG_TABLE", "log_coord"),
            table_path=os.environ.get("TABLE_PATH", "tables/demo"),
        )

    # ------------------------------------------------------------------
    # resource bootstrap (idempotent; safe to run from every process)
    # ------------------------------------------------------------------

    def _ensure_resources(self):
        try:
            self.s3.head_bucket(Bucket=self.bucket)
        except ClientError:
            try:
                self.s3.create_bucket(Bucket=self.bucket)
            except ClientError:
                pass
        for _ in range(40):
            try:
                descr = self.ddb.describe_table(TableName=self.log_table)
                if descr["Table"]["TableStatus"] == "ACTIVE":
                    return
            except ClientError:
                try:
                    self.ddb.create_table(
                        TableName=self.log_table,
                        KeySchema=[
                            {"AttributeName": "tablePath", "KeyType": "HASH"},
                            {"AttributeName": "fileName", "KeyType": "RANGE"},
                        ],
                        AttributeDefinitions=[
                            {"AttributeName": "tablePath", "AttributeType": "S"},
                            {"AttributeName": "fileName", "AttributeType": "S"},
                        ],
                        BillingMode="PAY_PER_REQUEST",
                    )
                except ClientError:
                    pass
            time.sleep(0.25)
        raise RuntimeError("coordination table %s never became ACTIVE" % self.log_table)

    # ------------------------------------------------------------------
    # object storage
    # ------------------------------------------------------------------

    def object_exists(self, key):
        _announce("probe", table=self.table_path, key=key)
        try:
            self.s3.head_object(Bucket=self.bucket, Key=key)
            present = True
        except ClientError:
            present = False
        return present

    def read_object(self, key):
        body = self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        _announce("read", table=self.table_path, key=key, size=len(body))
        return body

    def write_object(self, key, data):
        name = key.rsplit("/", 1)[-1]
        if _FINAL_KEY_RE.search(key):
            _announce("before_publish", table=self.table_path, key=key, file=name)
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data)
        _announce("wrote", table=self.table_path, key=key, size=len(data))

    def list_object_keys(self, prefix):
        keys = []
        token = None
        while True:
            kwargs = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            page = self.s3.list_objects_v2(**kwargs)
            keys.extend(item["Key"] for item in page.get("Contents", []))
            if not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken")
        _announce("listing", table=self.table_path, prefix=prefix, count=len(keys))
        return keys

    # ------------------------------------------------------------------
    # coordination entries
    # ------------------------------------------------------------------

    def get_entry(self, file_name):
        item = self.ddb.get_item(
            TableName=self.log_table,
            Key={"tablePath": {"S": self.table_path}, "fileName": {"S": file_name}},
            ConsistentRead=True,
        ).get("Item")
        entry = self._item_to_entry(item) if item else None
        _announce("entry_read", table=self.table_path, file=file_name,
                  complete=None if entry is None else entry["complete"])
        return entry

    def latest_entry(self):
        items = self.ddb.query(
            TableName=self.log_table,
            KeyConditionExpression="tablePath = :t",
            ExpressionAttributeValues={":t": {"S": self.table_path}},
            ScanIndexForward=False,
            Limit=1,
            ConsistentRead=True,
        ).get("Items", [])
        entry = self._item_to_entry(items[0]) if items else None
        _announce("latest_read", table=self.table_path,
                  file=None if entry is None else entry["fileName"],
                  complete=None if entry is None else entry["complete"])
        return entry

    def put_entry_new(self, entry):
        """Insert a coordination entry only if no entry exists for this version."""
        _announce("before_reserve", table=self.table_path, file=entry["fileName"],
                  temp=entry.get("tempPath"))
        try:
            self.ddb.put_item(
                TableName=self.log_table,
                Item=self._entry_to_item(entry),
                ConditionExpression="attribute_not_exists(fileName)",
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                raise EntryExists(entry["fileName"])
            raise

    def put_entry_existing(self, entry):
        """Overwrite the coordination entry for this version.

        Conditional, like the reservation: the overwrite lands only while the
        entry is still there and still records this entry's temp object, and
        raises EntryGone otherwise.
        """
        _announce("before_complete", table=self.table_path, file=entry["fileName"],
                  complete=entry.get("complete"))
        try:
            self.ddb.put_item(
                TableName=self.log_table,
                Item=self._entry_to_item(entry),
                ConditionExpression="attribute_exists(fileName) AND tempPath = :tp",
                ExpressionAttributeValues={":tp": {"S": entry["tempPath"]}},
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                raise EntryGone(entry["fileName"])
            raise

    # ------------------------------------------------------------------
    # entry <-> item mapping
    # ------------------------------------------------------------------

    @staticmethod
    def _entry_to_item(entry):
        item = {
            "tablePath": {"S": entry["tablePath"]},
            "fileName": {"S": entry["fileName"]},
            "tempPath": {"S": entry["tempPath"]},
            "complete": {"BOOL": bool(entry["complete"])},
        }
        if entry.get("expireTime") is not None:
            item["expireTime"] = {"N": str(int(entry["expireTime"]))}
        return item

    @staticmethod
    def _item_to_entry(item):
        expire = item.get("expireTime")
        return {
            "tablePath": item["tablePath"]["S"],
            "fileName": item["fileName"]["S"],
            "tempPath": item["tempPath"]["S"],
            "complete": bool(item["complete"]["BOOL"]),
            "expireTime": int(expire["N"]) if expire else None,
        }
