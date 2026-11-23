"""S3 client and the key layout of a strata bucket."""
import os
import re

import boto3
from botocore.config import Config

# Frame batch object name inside a generation: {first:012}-{last:012}-{unix_ts}.jsonl
BATCH_RE = re.compile(r"/(\d{12})-(\d{12})-(\d+)\.jsonl$")

# Largest object other than db.sqlite the bucket may hold (see README, "Bucket layout").
MAX_SMALL_OBJECT_BYTES = 4096


def client():
    endpoint = os.environ.get("S3_ENDPOINT")
    if not endpoint:
        raise SystemExit("S3_ENDPOINT is not set")
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "test"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "test"),
        config=Config(
            s3={"addressing_style": "path"},
            # strata owns its retry policy; SDK retries would stack hidden backoff
            # onto the commit path.
            retries={"total_max_attempts": 1, "mode": "standard"},
            connect_timeout=5,
            read_timeout=60,
            max_pool_connections=16,
        ),
    )


def generation_prefix(db_name, generation):
    return f"{db_name}-{generation}/"


def dep_key(db_name, generation):
    return f"{db_name}-{generation}/.dep"


def change_counter_key(db_name, generation):
    return f"{db_name}-{generation}/.changecounter"


def snapshot_key(db_name, generation):
    return f"{db_name}-{generation}/db.sqlite"


def batch_key(db_name, generation, first, last, ts):
    return f"{db_name}-{generation}/{first:012d}-{last:012d}-{ts}.jsonl"


def parse_frame_range(key):
    """Returns (first_frame_no, last_frame_no, timestamp) for a frame batch key, else None."""
    m = BATCH_RE.search(key)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def is_no_such_key(exc):
    code = getattr(exc, "response", {}).get("Error", {}).get("Code")
    return code in ("NoSuchKey", "404", "NotFound")


def list_keys(s3, bucket, prefix, max_keys=None):
    """Lists keys under prefix in S3 order (lexicographic). Stops after max_keys if given."""
    keys = []
    kwargs = {"Bucket": bucket, "Prefix": prefix}
    if max_keys is not None:
        kwargs["MaxKeys"] = max_keys
    while True:
        resp = s3.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []):
            keys.append(obj["Key"])
            if max_keys is not None and len(keys) >= max_keys:
                return keys
        if not resp.get("IsTruncated"):
            return keys
        kwargs["ContinuationToken"] = resp["NextContinuationToken"]
