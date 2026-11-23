"""Durable state for the object journal.

Layout in one DynamoDB table (string partition key ``pk``):

  CTRL        the fleet-wide phase book: phase, epoch, and the highest
              format_version a publication may carry right now
  HOST#<slot> what build a slot runs, established by the process at startup
  HEAD#<rid>  the authoritative pointer for a record: revision and blob key
  OP#<op_id>  receipt of one acknowledged operation

S3 holds immutable whole-record objects; nothing ever overwrites a blob. A
record becomes current only when its HEAD moves, and the HEAD moves only
inside the publication transaction below. Objects are written before the
transaction so a HEAD never names a missing object.
"""

import boto3
import botocore.config

CTRL_PK = "CTRL"
LIVE_FORMATS = {"idle": 1, "prepare": 1, "activate": 2}


class Store:
    def __init__(self, endpoint, region, bucket, table):
        self.bucket = bucket
        self.table = table
        cfg = botocore.config.Config(retries={"max_attempts": 2})
        self._s3 = boto3.client(
            "s3", endpoint_url=endpoint, region_name=region, config=cfg
        )
        self._ddb = boto3.client(
            "dynamodb", endpoint_url=endpoint, region_name=region, config=cfg
        )

    # -- control plane ----------------------------------------------------
    def create_table(self, name):
        try:
            self._ddb.create_table(
                TableName=name,
                KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
                AttributeDefinitions=[
                    {"AttributeName": "pk", "AttributeType": "S"}
                ],
                BillingMode="PAY_PER_REQUEST",
            )
            self._ddb.get_waiter("table_exists").wait(TableName=name)
        except Exception:
            pass

    def init_ctrl(self, phase="idle", epoch=1):
        self._ddb.put_item(
            TableName=self.table,
            Item={
                "pk": {"S": CTRL_PK},
                "phase": {"S": phase},
                "epoch": {"N": str(epoch)},
                "ceiling": {"N": str(LIVE_FORMATS[phase])},
            },
        )

    def read_ctrl(self):
        resp = self._ddb.get_item(
            TableName=self.table,
            Key={"pk": {"S": CTRL_PK}},
            ConsistentRead=True,
        )
        item = resp.get("Item")
        if not item:
            return None
        return {
            "phase": item["phase"]["S"],
            "epoch": int(item["epoch"]["N"]),
            "ceiling": int(item["ceiling"]["N"]),
        }

    def try_advance_ctrl(self, from_phase, from_epoch, to_phase, new_ceiling):
        """Move the control item; rejected unless we saw the current state.

        Every accepted move bumps the epoch, which fences anything still
        publishing against the previous control state.
        """
        try:
            self._ddb.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self.table,
                            "Key": {"pk": {"S": CTRL_PK}},
                            "ConditionExpression": (
                                "attribute_exists(#k) AND #p = :phase "
                                "AND #e = :epoch"
                            ),
                            "UpdateExpression": (
                                "SET #p = :new_phase, #e = :new_epoch, "
                                "#c = :ceiling"
                            ),
                            "ExpressionAttributeNames": {
                                "#k": "pk",
                                "#p": "phase",
                                "#e": "epoch",
                                "#c": "ceiling",
                            },
                            "ExpressionAttributeValues": {
                                ":phase": {"S": from_phase},
                                ":epoch": {"N": str(from_epoch)},
                                ":new_phase": {"S": to_phase},
                                ":new_epoch": {"N": str(from_epoch + 1)},
                                ":ceiling": {"N": str(new_ceiling)},
                            },
                        }
                    }
                ]
            )
            return True
        except Exception:
            return False

    # -- fleet ------------------------------------------------------------
    def put_host(self, slot, build, generation, reads, writes, run):
        self._ddb.put_item(
            TableName=self.table,
            Item={
                "pk": {"S": "HOST#%s" % slot},
                "build": {"S": build},
                "generation": {"N": str(generation)},
                "reads": {"NS": [str(f) for f in reads]},
                "writes": {"N": str(writes)},
                "run": {"S": run},
                "ready": {"BOOL": True},
            },
        )

    def list_hosts(self):
        resp = self._ddb.scan(
            TableName=self.table,
            FilterExpression="begins_with(#k, :prefix)",
            ExpressionAttributeNames={"#k": "pk"},
            ExpressionAttributeValues={":prefix": {"S": "HOST#"}},
            ConsistentRead=True,
        )
        hosts = []
        for item in resp.get("Items", []):
            hosts.append(
                {
                    "slot": item["pk"]["S"][5:],
                    "build": item["build"]["S"],
                    "generation": int(item["generation"]["N"]),
                    "reads": sorted(int(x) for x in item["reads"]["NS"]),
                    "writes": int(item["writes"]["N"]),
                }
            )
        return hosts

    # -- records ----------------------------------------------------------
    def read_head(self, rid):
        resp = self._ddb.get_item(
            TableName=self.table,
            Key={"pk": {"S": "HEAD#%s" % rid}},
            ConsistentRead=True,
        )
        item = resp.get("Item")
        if not item:
            return None
        return {
            "revision": int(item["revision"]["N"]),
            "blob_key": item["blob_key"]["S"],
            "fmt": int(item["fmt"]["N"]),
            "op_id": item.get("op_id", {}).get("S"),
        }

    def put_head(self, rid, blob_key, fmt, op_id):
        """Seed a new record; only used before any revision exists."""
        self._ddb.put_item(
            TableName=self.table,
            Item={
                "pk": {"S": "HEAD#%s" % rid},
                "revision": {"N": "0"},
                "blob_key": {"S": blob_key},
                "fmt": {"N": str(fmt)},
                "op_id": {"S": op_id},
            },
        )

    def put_blob(self, key, body):
        self._s3.put_object(Bucket=self.bucket, Key=key, Body=body.encode("utf-8"))

    def get_blob(self, key):
        try:
            resp = self._s3.get_object(Bucket=self.bucket, Key=key)
        except Exception:
            return None
        return resp["Body"].read().decode("utf-8")

    def publish(self, rid, blob_key, fmt, expect_revision, expect_epoch, op_id):
        """Move a record's head to a freshly written object.

        ``blob_key`` must already be in S3. The move is one transaction that
        succeeds only if the record is still at ``expect_revision``, control
        is still at ``expect_epoch`` and permits ``fmt``, and ``op_id`` has
        never been receipted. Returns the receipt, or None when rejected.
        """
        try:
            self._ddb.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self.table,
                            "Item": {
                                "pk": {"S": "HEAD#%s" % rid},
                                "revision": {"N": str(expect_revision + 1)},
                                "blob_key": {"S": blob_key},
                                "fmt": {"N": str(fmt)},
                                "op_id": {"S": op_id},
                            },
                            "ConditionExpression": (
                                "attribute_exists(#k) AND #r = :r"
                            ),
                            "ExpressionAttributeNames": {"#k": "pk", "#r": "revision"},
                            "ExpressionAttributeValues": {
                                ":r": {"N": str(expect_revision)}
                            },
                        }
                    },
                    {
                        "ConditionCheck": {
                            "TableName": self.table,
                            "Key": {"pk": {"S": CTRL_PK}},
                            "ConditionExpression": (
                                "attribute_exists(#k) AND #e = :e AND #c >= :f"
                            ),
                            "ExpressionAttributeNames": {
                                "#k": "pk", "#e": "epoch", "#c": "ceiling",
                            },
                            "ExpressionAttributeValues": {
                                ":e": {"N": str(expect_epoch)},
                                ":f": {"N": str(fmt)},
                            },
                        }
                    },
                    {
                        "Put": {
                            "TableName": self.table,
                            "Item": {
                                "pk": {"S": "OP#%s" % op_id},
                                "rid": {"S": rid},
                                "revision": {"N": str(expect_revision + 1)},
                                "blob_key": {"S": blob_key},
                                "fmt": {"N": str(fmt)},
                            },
                            "ConditionExpression": "attribute_not_exists(#k)",
                            "ExpressionAttributeNames": {"#k": "pk"},
                        }
                    },
                ]
            )
            return {"revision": expect_revision + 1, "blob_key": blob_key, "fmt": fmt}
        except Exception:
            return None

    def find_op(self, op_id):
        resp = self._ddb.get_item(
            TableName=self.table,
            Key={"pk": {"S": "OP#%s" % op_id}},
            ConsistentRead=True,
        )
        item = resp.get("Item")
        if not item:
            return None
        return {
            "rid": item["rid"]["S"],
            "revision": int(item["revision"]["N"]),
            "blob_key": item["blob_key"]["S"],
            "fmt": int(item["fmt"]["N"]),
        }
