#!/usr/bin/env python3
"""Seed a rehearsal chunk store: bucket objects, index snapshots, run rows.

Writes a manifest with the generated names so a local run can be checked
by hand. The graded environment creates its own fixtures with its own
names; nothing here is used by it.
"""
import argparse
import hashlib
import json
import os
import random
import secrets
import subprocess
import sys
import time
import uuid

import store
import boto3
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from infra.cfn_deploy import deploy


def chunk_entry(key, payload, expires_at):
    return {"key": key, "digest": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload), "expires_at": expires_at}


def seed(args):
    bucket = args.bucket or "cs-%s" % uuid.uuid4().hex[:12]
    table = args.table or "runs-%s" % uuid.uuid4().hex[:8]
    run = args.run_id or "r-%s" % uuid.uuid4().hex[:8]
    batch = "b-%s" % uuid.uuid4().hex[:8]
    tenant, table_id = "t-%s" % uuid.uuid4().hex[:4], "idx-%s" % uuid.uuid4().hex[:4]
    v0, v1 = "g%s" % uuid.uuid4().hex[:8], "g%s" % uuid.uuid4().hex[:8]
    table_id2 = "idx-%s" % uuid.uuid4().hex[:4]
    nv0 = "k%s" % uuid.uuid4().hex[:8]
    nv1 = "m%s" % uuid.uuid4().hex[:8]
    nv2 = "p%s" % uuid.uuid4().hex[:8]
    nv3 = "q%s" % uuid.uuid4().hex[:8]

    s3 = store.s3_client(args.endpoint, args.region)
    ddb = store.ddb_resource(args.endpoint, args.region)
    deploy(boto3.client("cloudformation", endpoint_url=args.endpoint, region_name=args.region),
           "seed-" + bucket + "-" + batch,
           Path(__file__).parent / "infra/seed.template.json",
           BucketName=bucket)
    ddb.create_table(
        TableName=table,
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"},
                   {"AttributeName": "sk", "KeyType": "RANGE"}],
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    ddb.Table(table).wait_until_exists()

    st = store.Store(args.endpoint, args.region, bucket, table, run)
    now = time.time()
    targets, live, staged = [], [], []
    for i in range(args.targets):
        key, payload = "chunks/%s" % secrets.token_hex(8), secrets.token_bytes(160)
        s3.put_object(Bucket=bucket, Key=key, Body=payload)
        targets.append(chunk_entry(key, payload, now - 500 - 40 * i))
    for i in range(args.live):
        key, payload = "chunks/%s" % secrets.token_hex(8), secrets.token_bytes(160)
        s3.put_object(Bucket=bucket, Key=key, Body=payload)
        live.append(chunk_entry(key, payload, now + 3600))
    for i in range(args.staged):
        key, payload = "chunks/%s" % secrets.token_hex(8), secrets.token_bytes(160)
        s3.put_object(Bucket=bucket, Key=key, Body=payload)
        staged.append({"key": key,
                       "digest": hashlib.sha256(payload).hexdigest()})

    index_key = "index/%s/%s/%s.json" % (tenant, table_id, v0)
    st.put_json(index_key, {"version": v0,
                            "chunks": targets + live})
    other_key = "index/%s/%s/%s.json" % (tenant, table_id2, nv0)
    st.put_json(other_key, {"version": nv0, "chunks": targets})
    st.put_item({"sk": "HEAD", "version": v0, "index_key": index_key})
    st.put_item({"sk": "HEAD#%s" % table_id2, "version": nv0,
                 "index_key": other_key})
    st.put_item({"sk": "OWNER", "epoch": 1, "worker": ""})
    st.put_item({"sk": "META", "run": run, "tenant": tenant,
                 "table_id": table_id, "batch": batch, "cutoff": now,
                 "delete_delay_s": args.delay, "poll_s": 0.1,
                 "chunk_prefix": "chunks", "index_prefix": "index",
                 "marker_prefix": "markers", "out_version": v1})

    manifest = {"bucket": bucket, "table": table, "run": run, "batch": batch,
                "tenant": tenant, "table_id": table_id,
                "in_version": v0, "out_version": v1,
                "targets": [e["key"] for e in targets],
                "live": [e["key"] for e in live],
                "staged": [e["key"] for e in staged],
                "digests": {e["key"]: e["digest"] for e in targets + live}}
    with open(args.out, "w") as fh:
        json.dump(manifest, fh, indent=2)
    print(json.dumps(manifest, indent=2))

    # the table's own later snapshots, derived from the entries it serves
    edge = now - 520.0
    snapshots = [
        [nv1, [e for e in targets if e["expires_at"] > now]],
        [nv2, [e for e in targets if e["expires_at"] >= edge]],
        [nv3, [e for e in targets
               if e["expires_at"] >= edge and e["expires_at"] > now]],
    ]

    turnover_args = [sys.executable, os.path.abspath(__file__), "--turnover",
                     json.dumps({
                         "endpoint": args.endpoint, "region": args.region,
                         "bucket": bucket, "table": table, "run": run,
                         "tenant": tenant, "table_id": table_id2,
                         "snapshots": snapshots,
                         "from_version": nv0,
                     })]
    subprocess.Popen(turnover_args, start_new_session=True)
    return manifest


def turnover(payload):
    """Publish the given index snapshots one after another, on this
    table's own clock, keeping its pointer current as it goes."""
    args = json.loads(payload)
    st = store.Store(args["endpoint"], args["region"], args["bucket"],
                     args["table"], args["run"])
    rng = random.Random()
    when = time.time() + rng.uniform(8.0, 14.0)
    prev_version = args["from_version"]
    try:
        for i, (version, chunks) in enumerate(args["snapshots"]):
            if i:
                when += rng.uniform(3.0, 9.0)
            time.sleep(max(0.0, when - time.time()))
            key = "index/%s/%s/%s.json" % (args["tenant"],
                                           args["table_id"], version)
            st.put_json(key, {"version": version, "chunks": chunks})
            for _ in range(10):
                try:
                    st.table.update_item(
                        Key={"pk": "RUN#%s" % args["run"],
                             "sk": "HEAD#%s" % args["table_id"]},
                        ConditionExpression="#v = :was",
                        UpdateExpression="SET #v = :nowv, #k = :key",
                        ExpressionAttributeNames={"#v": "version",
                                                  "#k": "index_key"},
                        ExpressionAttributeValues={":was": prev_version,
                                                   ":nowv": version,
                                                   ":key": key},
                    )
                    break
                except Exception:
                    time.sleep(0.2)
            prev_version = version
            print("index table %s now serves %s" % (args["table_id"],
                                                    version))
            sys.stdout.flush()
    except Exception as exc:
        sys.stderr.write("turnover stopped: %r\n" % (exc,))
        sys.stderr.flush()


def main():
    ap = argparse.ArgumentParser(description="seed a rehearsal chunk store")
    ap.add_argument("--endpoint", default="http://localhost:4566")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--bucket", default=None)
    ap.add_argument("--table", default=None)
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--targets", type=int, default=2)
    ap.add_argument("--live", type=int, default=2)
    ap.add_argument("--staged", type=int, default=1)
    ap.add_argument("--delay", type=float, default=8.0)
    ap.add_argument("--turnover", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--out", default="manifest.json")
    args = ap.parse_args()

    if args.turnover is not None:
        turnover(args.turnover)
        return
    seed(args)


if __name__ == "__main__":
    main()
