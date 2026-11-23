"""Live discovery of what is still encrypted under a key.

Reads the account as it is now, not the scan's queue: consumers kept by filters, consumers the
scan never saw, and consumers created since the scan all count. Any error while reading means the
answer is unknown, and raises DiscoveryError rather than returning a partial list.
"""
import re

from botocore.exceptions import ClientError

from .aws import client, error_code
from .s3_envelope_object import KEY_METADATA, consumer_id

_KEY_ID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$|^mrk-[0-9a-fA-F]{32}$")


class DiscoveryError(Exception):
    pass


def _key_id(ref, resolved):
    """Key ID for any form a consumer may name its key by (key ID, key ARN, alias, alias ARN)."""
    if ref in resolved:
        return resolved[ref]
    if _KEY_ID.match(ref):
        key_id = ref
    elif ref.startswith("arn:") and ":key/" in ref:
        key_id = ref.rsplit("/", 1)[1]
    else:
        try:
            key_id = client("kms").describe_key(KeyId=ref)["KeyMetadata"]["KeyId"]
        except ClientError as err:
            if error_code(err) == "NotFoundException":
                key_id = None  # names no key at all
            else:
                raise DiscoveryError(f"cannot resolve key reference {ref}: {error_code(err) or err}")
    resolved[ref] = key_id
    return key_id


def live_consumers(key_id):
    resolved = {}
    found = []
    try:
        sm = client("secretsmanager")
        for page in sm.get_paginator("list_secrets").paginate(IncludePlannedDeletion=True):
            for s in page.get("SecretList", []):
                ref = s.get("KmsKeyId")
                if ref and _key_id(ref, resolved) == key_id:
                    found.append(s["Name"])

        s3 = client("s3")
        for bucket in s3.list_buckets().get("Buckets", []):
            name = bucket["Name"]
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=name):
                for obj in page.get("Contents", []):
                    try:
                        head = s3.head_object(Bucket=name, Key=obj["Key"])
                    except ClientError as err:
                        if error_code(err) in ("404", "NoSuchKey", "NotFound"):
                            continue
                        raise
                    ref = head.get("Metadata", {}).get(KEY_METADATA)
                    if ref and _key_id(ref, resolved) == key_id:
                        found.append(consumer_id(name, obj["Key"]))
    except DiscoveryError:
        raise
    except Exception as err:
        raise DiscoveryError(f"{type(err).__name__}: {error_code(err) or err}")
    return found
