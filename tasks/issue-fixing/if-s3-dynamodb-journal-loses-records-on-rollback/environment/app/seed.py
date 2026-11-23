"""Create a fresh journal: bucket, table, control item and starting records.

Each starting record holds the counter plus a few attributes left there by
older writers -- the values the fleet currently just carries around.
"""

import argparse
import hashlib
import json
import os
import random
import string
import uuid
import sys
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parent))
from infra.cfn_deploy import deploy

import records
import store

VALUES = [
    "west-12",
    {"env": ["a", "b"], "rank": 3},
    [1, 4, 9],
    {"ttl": {"days": 30, "renew": True}},
    "owner=relay;stage=2",
    {"check": {"sum": "9f2a", "parts": [2, 5]}},
]


def make_attrs(rng, count):
    attrs = {}
    for i in range(count):
        name = "%s_%s" % (
            "".join(rng.choice(string.ascii_lowercase) for _ in range(3)),
            "".join(rng.choice(string.hexdigits.lower()[:16]) for _ in range(4)),
        )
        attrs[name] = rng.choice(VALUES)
    return attrs


def main():
    parser = argparse.ArgumentParser(description="create a fresh journal")
    parser.add_argument("--endpoint", default=os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566"))
    parser.add_argument("--region", default=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--table", required=True)
    parser.add_argument("--count", type=int, default=8)
    parser.add_argument("--counter", type=int, default=10)
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    st = store.Store(args.endpoint, args.region, args.bucket, args.table)
    cfn = boto3.client("cloudformation", endpoint_url=args.endpoint, region_name=args.region)
    stack_id = hashlib.sha256((args.bucket + ":" + args.table).encode()).hexdigest()[:24]
    deploy(cfn, "journal-seed-%s" % stack_id,
           Path(__file__).parent / "infra/seed.template.json",
           BucketName=args.bucket, TableName=args.table)
    st.init_ctrl("idle", epoch=1)

    created = []
    for i in range(args.count):
        rid = "rec-%02d" % i
        rec = records.Record(args.counter, make_attrs(rng, rng.randint(2, 4)))
        blob_key = "rec/%s/%s" % (rid, uuid.uuid4().hex)
        st.put_blob(blob_key, records.encode(rec, records.XML_FORMAT))
        st.put_head(rid, blob_key, records.XML_FORMAT, "seed")
        created.append({"record_id": rid, "attrs": sorted(rec.attrs)})
    print(json.dumps({"bucket": args.bucket, "table": args.table, "records": created}, indent=1))


if __name__ == "__main__":
    main()
