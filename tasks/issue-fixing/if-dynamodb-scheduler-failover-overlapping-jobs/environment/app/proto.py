"""State-plane adapter: one DynamoDB table, one item layout, one clock.

Everything the processes share lives in the state table named on the command
line.  The layout is part of the deployed contract (see README.md); the helpers
below are deliberately thin so that the policy that uses them stays readable.
"""

import argparse
import json
import os
import time
import uuid

import boto3
from botocore.config import Config

# Tuning the program itself needs.  Anything not listed here is a property of
# the outside world and is not the program's business.
CONFIG = {
    "poll_s": 0.1,            # scheduler / worker loop step
    "leader_lease_s": 3.0,    # CLUSTER/LEADER validity
    "run_lease_s": 6.0,       # per-job ACTIVE validity
    "heartbeat_s": 1.0,       # worker renewal period
    "decision_window_s": 2.0, # a nominal is decided within this or discarded
    "short_body_s": 0.5,      # body duration once the callback acknowledges
    "sdk_max_attempts": 2,
    "sdk_timeout_s": 1.0,
}

STATE_TABLE_DEFAULT = "cadence_state"


def now() -> float:
    return time.time()


def new_id() -> str:
    return uuid.uuid4().hex


def client(endpoint=None):
    endpoint = endpoint or os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566")
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    return boto3.client(
        "dynamodb",
        endpoint_url=endpoint,
        region_name=os.environ["AWS_DEFAULT_REGION"],
        config=Config(
            retries={"max_attempts": CONFIG["sdk_max_attempts"], "mode": "standard"},
            connect_timeout=CONFIG["sdk_timeout_s"],
            read_timeout=CONFIG["sdk_timeout_s"],
        ),
    )


def val(v):
    """A single encoded value, for use inside an expression attribute map."""
    return encode({"_": v})["_"]


def cond_failed(err):
    """True when the service rejected the write's condition, nothing else."""
    return err.response.get("Error", {}).get("Code") in (
        "ConditionalCheckFailedException", "TransactionCanceledException")


class State:
    """Thin typed wrapper over the state table."""

    def __init__(self, table, endpoint=None):
        self.table = table
        self.db = client(endpoint)

    # -- table lifecycle ---------------------------------------------------
    def create(self, table=None):
        table = table or self.table
        existing = self.db.list_tables().get("TableNames", [])
        if table in existing:
            return False
        self.db.create_table(
            TableName=table,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"},
                       {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                  {"AttributeName": "sk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        self.db.get_waiter("table_exists").wait(
            TableName=table, WaiterConfig={"Delay": 1, "MaxAttempts": 30})
        return True

    # -- primitives --------------------------------------------------------
    def get(self, pk, sk, consistent=True):
        resp = self.db.get_item(
            TableName=self.table, Key={"pk": {"S": pk}, "sk": {"S": sk}},
            ConsistentRead=consistent,
        )
        item = resp.get("Item")
        return decode(item) if item else None

    def put(self, pk, sk, attrs, condition=None, names=None, values=None):
        item = {"pk": {"S": pk}, "sk": {"S": sk}}
        item.update(encode(attrs))
        kwargs = {"TableName": self.table, "Item": item}
        if condition:
            kwargs["ConditionExpression"] = condition
            if names:
                kwargs["ExpressionAttributeNames"] = names
            if values:
                kwargs["ExpressionAttributeValues"] = values
        self.db.put_item(**kwargs)

    def update(self, pk, sk, attrs, condition=None, names=None, values=None):
        names = dict(names or {})
        values = dict(values or {})
        sets = []
        for k in attrs:
            names["#" + k] = k
            sets.append("#" + k + " = :" + k)
            values[":" + k] = val(attrs[k])
        kwargs = {
            "TableName": self.table,
            "Key": {"pk": {"S": pk}, "sk": {"S": sk}},
            "UpdateExpression": "SET " + ", ".join(sets),
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": values,
        }
        if condition:
            kwargs["ConditionExpression"] = condition
        self.db.update_item(**kwargs)

    def delete(self, pk, sk, condition=None, names=None, values=None):
        kwargs = {"TableName": self.table, "Key": {"pk": {"S": pk}, "sk": {"S": sk}}}
        if condition:
            kwargs["ConditionExpression"] = condition
            if names:
                kwargs["ExpressionAttributeNames"] = names
            if values:
                kwargs["ExpressionAttributeValues"] = values
        self.db.delete_item(**kwargs)

    def transact(self, ops):
        """All-or-nothing over put/update/delete descriptors.

        Each op: {"kind": "put"|"update"|"delete", "pk":..., "sk":...,
        "attrs": {...} (put/update), "condition": ..., "names": {...},
        "values": {...}}.
        """
        items = []
        for o in ops:
            key = {"pk": {"S": o["pk"]}, "sk": {"S": o["sk"]}}
            kind = o["kind"]
            desc = {"TableName": self.table,
                    "Item" if kind == "put" else "Key": key}
            if kind == "put":
                desc["Item"].update(encode(o.get("attrs", {})))
            elif kind == "update":
                names, values, sets = {}, {}, []
                for k in o.get("attrs", {}):
                    names["#" + k] = k
                    sets.append("#" + k + " = :" + k)
                    values[":" + k] = val(o["attrs"][k])
                desc["UpdateExpression"] = "SET " + ", ".join(sets)
                desc["ExpressionAttributeNames"] = names
                desc["ExpressionAttributeValues"] = values
            if o.get("condition"):
                desc["ConditionExpression"] = o["condition"]
                if o.get("names"):
                    desc["ExpressionAttributeNames"] = o["names"]
                if o.get("values"):
                    desc["ExpressionAttributeValues"] = o["values"]
            items.append({kind.capitalize(): desc})
        self.db.transact_write_items(TransactItems=items)

    def query(self, pk, sk_prefix=None):
        kwargs = {
            "TableName": self.table,
            "KeyConditionExpression": "#p = :p",
            "ExpressionAttributeNames": {"#p": "pk"},
            "ExpressionAttributeValues": {":p": {"S": pk}},
            "ConsistentRead": True,
        }
        if sk_prefix:
            kwargs["KeyConditionExpression"] += " AND begins_with(#s, :s)"
            kwargs["ExpressionAttributeNames"]["#s"] = "sk"
            kwargs["ExpressionAttributeValues"][":s"] = {"S": sk_prefix}
        items, exclusive = [], None
        while True:
            if exclusive:
                kwargs["ExclusiveStartKey"] = exclusive
            resp = self.db.query(**kwargs)
            items.extend(resp.get("Items", []))
            exclusive = resp.get("LastEvaluatedKey")
            if not exclusive:
                return [decode(i) for i in items]

    # -- keys --------------------------------------------------------------
    @staticmethod
    def job_key(job):
        return "JOB#" + job

    @staticmethod
    def slot_sk(slot):
        return "SLOT#%04d" % slot

    @staticmethod
    def run_sk(run_id):
        return "RUN#" + run_id

    def active(self, job):
        return self.get(self.job_key(job), "ACTIVE")

    def slot(self, job, slot):
        return self.get(self.job_key(job), self.slot_sk(slot))

    def cursor(self, job):
        item = self.get(self.job_key(job), "CURSOR")
        return item["next_slot"] if item else 0


# -- attribute codecs -------------------------------------------------------
def encode(attrs):
    out = {}
    for k, v in attrs.items():
        if isinstance(v, bool):
            out[k] = {"BOOL": v}
        elif isinstance(v, (int, float)):
            out[k] = {"N": repr(v)}
        elif isinstance(v, (list, tuple)):
            out[k] = {"L": [{"S": str(x)} for x in v]}
        elif v is None:
            out[k] = {"NULL": True}
        else:
            out[k] = {"S": str(v)}
    return out


def decode(item):
    out = {}
    for k, v in item.items():
        if "S" in v:
            out[k] = v["S"]
        elif "N" in v:
            f = float(v["N"])
            out[k] = int(f) if f.is_integer() else f
        elif "BOOL" in v:
            out[k] = v["BOOL"]
        elif "L" in v:
            out[k] = [x.get("S") for x in v["L"]]
        elif "NULL" in v:
            out[k] = None
    return out


def main():
    ap = argparse.ArgumentParser(description="state-plane helpers")
    ap.add_argument("--state-table", default=STATE_TABLE_DEFAULT)
    ap.add_argument("--endpoint", default=None)
    ap.add_argument("command", choices=["init", "dump"])
    ap.add_argument("--pk", default=None, help="partition to dump")
    args = ap.parse_args()
    st = State(args.state_table, args.endpoint)
    if args.command == "init":
        created = st.create()
        print(json.dumps({"table": args.state_table, "created": created}))
    elif args.pk:
        for row in st.query(args.pk):
            print(json.dumps(row, sort_keys=True))


if __name__ == "__main__":
    main()
