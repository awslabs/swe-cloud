"""S3EnvelopeObject: an object in any bucket of the account.

Envelope objects carry KMS ciphertext as their body and name the key it was produced under in the
user metadata entry `kms-key-id` (see README, "Envelope convention"). Objects without that entry are
plain objects; they are backed up by digest and deleted like any other.
"""
from botocore.exceptions import ClientError

from libnuke.registry import Registration

from .aws import client, error_code
from .backup import decrypt_envelope, write_backup

TYPE = "S3EnvelopeObject"
KEY_METADATA = "kms-key-id"


def consumer_id(bucket, key):
    return f"s3://{bucket}/{key}"


class S3EnvelopeObject:
    def __init__(self, bucket, key, kms_key_id, settings):
        self.bucket = bucket
        self.key = key
        self.kms_key_id = kms_key_id
        self.settings = settings

    def string(self):
        return consumer_id(self.bucket, self.key)

    def properties(self):
        return {"Bucket": self.bucket, "Key": self.key, "KmsKeyId": self.kms_key_id or ""}

    def remove(self):
        s3 = client("s3")
        body = s3.get_object(Bucket=self.bucket, Key=self.key)["Body"].read()
        if self.kms_key_id:
            plaintext, key_arn = decrypt_envelope(body, self.string())
        else:
            plaintext, key_arn = body, None
        write_backup(self.settings, TYPE, self.string(), plaintext, key_arn)
        s3.delete_object(Bucket=self.bucket, Key=self.key)


class S3EnvelopeObjectLister:
    def list(self, settings):
        s3 = client("s3")
        resources = []
        for bucket in s3.list_buckets().get("Buckets", []):
            name = bucket["Name"]
            try:
                resources.extend(self._list_bucket(s3, name, settings))
            except ClientError as err:
                # A bucket we cannot read is skipped, not fatal: one bad bucket must not stop the
                # sweep of every other bucket.
                print(f"WARN {TYPE}: skipping bucket {name}: {error_code(err) or err}", flush=True)
        return resources

    def _list_bucket(self, s3, bucket, settings):
        found = []
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
            for obj in page.get("Contents", []):
                try:
                    head = s3.head_object(Bucket=bucket, Key=obj["Key"])
                except ClientError as err:
                    if error_code(err) in ("404", "NoSuchKey", "NotFound"):
                        continue  # deleted since the listing
                    raise
                kms_key_id = head.get("Metadata", {}).get(KEY_METADATA)
                found.append(S3EnvelopeObject(bucket, obj["Key"], kms_key_id, settings))
        return found


REGISTRATION = Registration(TYPE, S3EnvelopeObjectLister())

