"""The table commit every lock holder makes: take the next sequence number under the lock.

This is the critical section the lock protects: two holders at once produce a duplicate or lost
sequence number.

The counter write is conditional on the version of the counter this commit read (If-Match its
ETag). The lock alone cannot stop a write that the gateway answered 504 and applied later, after
this writer released and another committed; the condition makes such a late write a 412 instead
of a rollback. A 412 or an ambiguous answer on our own write is resolved by reading the counter
back: under the lock nobody else can move it to our seq, so finding our seq means it landed.
"""
import json
import time

import boto3
from botocore.exceptions import BotoCoreError, ClientError

COUNTER_MAX_ATTEMPTS = 8
COUNTER_RETRY_DELAY_S = 0.15


class LedgerConflict(Exception):
    pass


def _status(err):
    return int(err.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))


def _read_counter(s3, bucket, key):
    resp = s3.get_object(Bucket=bucket, Key=key)
    return int(json.loads(resp["Body"].read())["seq"]), resp["ETag"]


def _write_counter(s3, bucket, key, seq, read_etag):
    body = json.dumps({"seq": seq}).encode()
    for _ in range(COUNTER_MAX_ATTEMPTS):
        try:
            s3.put_object(Bucket=bucket, Key=key, Body=body, IfMatch=read_etag,
                          ContentType="application/json")
            return
        except ClientError as e:
            if _status(e) not in (412,) and _status(e) < 500:
                raise
        except BotoCoreError:
            pass
        # 412, 5xx or a dropped connection: read back on a fresh connection (the emulator's
        # keep-alive connection is unusable after a 412).
        s3 = boto3.client("s3")
        try:
            now_seq, now_etag = _read_counter(s3, bucket, key)
        except (ClientError, BotoCoreError):
            time.sleep(COUNTER_RETRY_DELAY_S)
            continue
        if now_seq == seq:
            return                      # our write landed
        if now_etag != read_etag:
            raise LedgerConflict(f"counter moved to {now_seq} while committing seq {seq}")
        time.sleep(COUNTER_RETRY_DELAY_S)   # not landed (yet): same write, same condition
    raise LedgerConflict(f"counter write for seq {seq} unresolved")


def commit(s3, bucket: str, ledger_prefix: str, worker: str, section: int, lock_etag: str,
           hold_ms: int) -> int:
    counter_key = f"{ledger_prefix}counter.json"
    current, counter_etag = _read_counter(s3, bucket, counter_key)
    seq = current + 1
    time.sleep(hold_ms / 1000.0)  # the commit's own work
    entry = {"seq": seq, "worker": worker, "section": section, "lock_etag": lock_etag}
    s3.put_object(Bucket=bucket, Key=f"{ledger_prefix}entries/{seq:06d}-{worker}-{section}.json",
                  Body=json.dumps(entry).encode(), ContentType="application/json")
    _write_counter(s3, bucket, counter_key, seq, counter_etag)
    return seq
