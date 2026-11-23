#!/usr/bin/env python3
"""Create a table's bucket, seed its lock object (released) and its ledger counter.

    python3 /app/bootstrap.py --bucket lakehouse-orders --lock-key orders/_locks/table_lock.json \
        --ledger-prefix orders/ledger/

Re-running it resets the table: the lock object, the counter and the ledger entries.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from infra.cfn_deploy import deploy

import boto3


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--bucket", default="lakehouse-orders")
    p.add_argument("--lock-key", default="orders/_locks/table_lock.json")
    p.add_argument("--ledger-prefix", default="orders/ledger/")
    p.add_argument("--results-prefix", default="orders/results/")
    args = p.parse_args()

    s3 = boto3.client("s3")
    existing = [b["Name"] for b in s3.list_buckets().get("Buckets", [])]
    if args.bucket not in existing:
        deploy(boto3.client("cloudformation"), "lock-app-seed-" + args.bucket,
               Path(__file__).parent / "infra/seed.template.json", BucketName=args.bucket)
    for prefix in (args.ledger_prefix, args.results_prefix):
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=args.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                s3.delete_object(Bucket=args.bucket, Key=obj["Key"])
    seed = {"expired": True, "validUntil": 0, "owner": "bootstrap"}
    s3.put_object(Bucket=args.bucket, Key=args.lock_key, Body=json.dumps(seed).encode(),
                  ContentType="application/json")
    s3.put_object(Bucket=args.bucket, Key=f"{args.ledger_prefix}counter.json",
                  Body=json.dumps({"seq": 0}).encode(), ContentType="application/json")
    print(f"seeded s3://{args.bucket}/{args.lock_key}")


if __name__ == "__main__":
    main()
