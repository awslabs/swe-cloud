"""The table commit every lock holder makes: take the next sequence number under the lock.

This is the critical section the lock protects: it reads the counter, does the commit's work and
writes the next value back, so two holders at once produce a duplicate or lost sequence number.
"""
import json
import time


def commit(s3, bucket: str, ledger_prefix: str, worker: str, section: int, lock_etag: str,
           hold_ms: int) -> int:
    counter_key = f"{ledger_prefix}counter.json"
    current = json.loads(s3.get_object(Bucket=bucket, Key=counter_key)["Body"].read())
    seq = int(current["seq"]) + 1
    time.sleep(hold_ms / 1000.0)  # the commit's own work
    entry = {"seq": seq, "worker": worker, "section": section, "lock_etag": lock_etag}
    s3.put_object(Bucket=bucket, Key=f"{ledger_prefix}entries/{seq:06d}-{worker}-{section}.json",
                  Body=json.dumps(entry).encode(), ContentType="application/json")
    s3.put_object(Bucket=bucket, Key=counter_key, Body=json.dumps({"seq": seq}).encode(),
                  ContentType="application/json")
    return seq
