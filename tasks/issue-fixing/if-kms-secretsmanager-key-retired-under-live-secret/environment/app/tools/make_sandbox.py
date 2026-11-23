#!/usr/bin/env python3
"""Create a small demo sandbox to sweep, and write a sweep config for it.

    python3 /app/tools/make_sandbox.py [--prefix demo] [--config-out sweep-config.json]

Talks to AWS_ENDPOINT_URL directly (the emulator), the way the owning teams create their
resources. Creates two customer managed keys, a secret and envelope objects encrypted under them
(following the envelope convention in README.md), a bucket the config keeps, and the backup bucket.
"""
import argparse
import hashlib
import sys
import uuid
from pathlib import Path
import base64
import json
import os
import secrets

import boto3
from botocore.config import Config

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from infra.cfn_deploy import deploy


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prefix", default="demo")
    parser.add_argument("--config-out", default="sweep-config.json")
    args = parser.parse_args()
    p = args.prefix

    kw = dict(region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
              endpoint_url=os.environ.get("AWS_ENDPOINT_URL"),
              config=Config(s3={"addressing_style": "path"}))
    kms, sm, s3 = (boto3.client(s, **kw) for s in ("kms", "secretsmanager", "s3"))

    data, keep, backups = f"{p}-data", f"{p}-keep", f"{p}-backups"
    for bucket in (data, keep, backups):
        deploy(boto3.client("cloudformation", **kw),
               "sandbox-bucket-" + hashlib.sha256(bucket.encode()).hexdigest()[:24],
               Path(__file__).resolve().parents[1] / "infra/seed.template.json", BucketName=bucket)

    keys = deploy(boto3.client("cloudformation", **kw), "sandbox-keys-" + uuid.uuid4().hex,
                  Path(__file__).resolve().parents[1] / "infra/keys.template.json", Prefix=p)
    app_key = {"KeyId": keys["AppId"], "Arn": keys["AppArn"]}
    cache_key = {"KeyId": keys["CacheId"], "Arn": keys["CacheArn"]}

    def envelope(key_ref, consumer):
        return kms.encrypt(KeyId=key_ref, Plaintext=secrets.token_bytes(32),
                           EncryptionContext={"consumer": consumer})["CiphertextBlob"]

    name = f"{p}/app/db-password"
    sm.create_secret(Name=name, KmsKeyId=app_key["Arn"],
                     SecretString=base64.b64encode(envelope(app_key["Arn"], name)).decode())

    objects = [
        (data, "exports/2026-10-01.bin", app_key["Arn"]),     # current writers record the key ARN
        (data, "cache/session-blob.bin", cache_key["KeyId"]),  # older writers recorded the key ID
        (keep, "reference/golden.bin", cache_key["Arn"]),
    ]
    for bucket, key, key_ref in objects:
        s3.put_object(Bucket=bucket, Key=key, Body=envelope(key_ref, f"s3://{bucket}/{key}"),
                      Metadata={"kms-key-id": key_ref})

    config = {
        "resource_types": ["S3EnvelopeObject", "SecretsManagerSecret", "KMSKey"],
        "filters": {
            "S3EnvelopeObject": [
                {"property": "Bucket", "value": keep},
                {"property": "Bucket", "value": backups},
            ],
        },
        "backup_bucket": backups,
        "settings": {"KMSKey": {"pending_window_days": 7}},
        "pass_sleep_s": 1,
        "feature_flags": {"wait_on_dependencies": False},
    }
    with open(args.config_out, "w") as fh:
        json.dump(config, fh, indent=2)
    print(f"created keys {app_key['KeyId']} {cache_key['KeyId']}; config written to {args.config_out}")


if __name__ == "__main__":
    main()
