"""Backup-before-delete.

Every consumer is backed up before it is deleted: its envelope is decrypted under the encryption
context it was written with, and a manifest recording the plaintext digest is written to the backup
bucket (settings.backup_bucket). A consumer that cannot be decrypted cannot be backed up, and is not
deleted.
"""
import datetime
import hashlib
import json
import urllib.parse

from .aws import client

ENCRYPTION_CONTEXT_KEY = "consumer"


def decrypt_envelope(ciphertext, consumer_id):
    out = client("kms").decrypt(CiphertextBlob=ciphertext,
                                EncryptionContext={ENCRYPTION_CONTEXT_KEY: consumer_id})
    return out["Plaintext"], out["KeyId"]


def write_backup(settings, consumer_type, consumer_id, plaintext, key_arn):
    bucket = settings.get("backup_bucket")
    if not bucket:
        raise RuntimeError("no backup_bucket configured; refusing to delete without a backup")
    record = {
        "consumer": consumer_id,
        "type": consumer_type,
        "key_arn": key_arn,
        "plaintext_sha256": hashlib.sha256(plaintext).hexdigest(),
        "size": len(plaintext),
        "taken_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    key = f"{consumer_type}/{urllib.parse.quote(consumer_id, safe='')}.json"
    client("s3").put_object(Bucket=bucket, Key=key, Body=json.dumps(record).encode(),
                            ContentType="application/json")
