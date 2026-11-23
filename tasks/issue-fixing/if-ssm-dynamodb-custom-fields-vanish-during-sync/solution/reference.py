#!/usr/bin/env python3
"""aft-sync: apply an account request's custom fields to the member account.

One invocation stands in for one provisioning execution's account-metadata
task. It is started whenever an account request record is committed, and it
converges the member account's custom-field parameters — one String parameter
per field under /aft/account-request/custom-fields/ — to the account request.

Usage:
    aft-sync --payload <file.json>

The payload file is the execution input: a snapshot of the aft-request item
at the commit that triggered this invocation. Shape:

    {
      "customization_request_id": "<uuid>",
      "account_info": {"account": {"id": "<12-digit account id>"}},
      "account_request": {
        "id": "<account email>",
        "custom_fields": "<JSON-encoded map of field key to string value>",
        "record_version": <int>
      }
    }

The AWS endpoint is taken from AWS_ENDPOINT_URL (region us-east-1).
Exit status 0 on success, 1 on failure.
"""
import argparse
import inspect
import json
import logging
import os
import sys
import threading
import time
import uuid
from typing import Any, Dict, List, Tuple

import boto3
from botocore.config import Config

from constants import (
    ACCOUNT_REQUEST_TABLE,
    DELETE_PARAMETER_BATCH_SIZE,
    SSM_PARAMETER_PATH,
    SYNC_STATE_TABLE,
)

logger = logging.getLogger("aft")

LEASE_MS = 5_000            # exclusion lease lifetime between heartbeats
RENEW_INTERVAL_S = 1.5      # lease heartbeat period while the holder works
POLL_MS = 0.2               # retry period while the account is busy
RUN_BUDGET_S = 27.0         # finish well inside the invocation bound


def service_client(service: str, region: str = "us-east-1"):
    return boto3.client(
        service,
        region_name=region,
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL"),
        config=Config(
            retries={"max_attempts": 3, "mode": "standard"},
            read_timeout=70,
            connect_timeout=5,
        ),
    )


def now_ms() -> int:
    return int(time.time() * 1000)


def lock_key(account_id: str) -> str:
    return f"lock#{account_id}"


def applied_key(account_id: str) -> str:
    return f"applied#{account_id}"


def acquire_lease(ddb, account_id: str, deadline: float):
    """Take this account's exclusion lease, waiting out any live holder.

    The lease expires on its own, so a holder that dies without releasing
    does not block the account forever; a live holder keeps it renewed."""
    owner = str(uuid.uuid4())
    while time.monotonic() < deadline:
        try:
            ddb.put_item(
                TableName=SYNC_STATE_TABLE,
                Item={
                    "pk": {"S": lock_key(account_id)},
                    "holder": {"S": owner},
                    "lease_expires_at": {"N": str(now_ms() + LEASE_MS)},
                },
                ConditionExpression=(
                    "attribute_not_exists(pk) OR #exp < :now"
                ),
                ExpressionAttributeNames={"#exp": "lease_expires_at"},
                ExpressionAttributeValues={":now": {"N": str(now_ms())}},
            )
            return owner
        except ddb.exceptions.ConditionalCheckFailedException:
            time.sleep(POLL_MS)
    return None


def release_lease(ddb, account_id: str, owner: str) -> None:
    try:
        ddb.delete_item(
            TableName=SYNC_STATE_TABLE,
            Key={"pk": {"S": lock_key(account_id)}},
            ConditionExpression="#h = :me",
            ExpressionAttributeNames={"#h": "holder"},
            ExpressionAttributeValues={":me": {"S": owner}},
        )
    except ddb.exceptions.ConditionalCheckFailedException:
        pass  # the lease expired and was taken over; it is no longer ours


class LeaseHeartbeat(threading.Thread):
    """Keeps this holder's lease alive while it is alive.

    A fixed lease cannot both outlast a request that sits in flight for many
    seconds and be taken over promptly when its holder dies, so the lease is
    short and renewed by a live holder. The renewal is conditional on the
    holder's identity, so a lease that was taken over after expiry is not
    resurrected."""

    def __init__(self, ddb, account_id: str, owner: str):
        super().__init__(daemon=True)
        self.ddb = ddb
        self.account_id = account_id
        self.owner = owner
        self.stop_event = threading.Event()
        self.lost = False

    def renew(self) -> bool:
        try:
            self.ddb.update_item(
                TableName=SYNC_STATE_TABLE,
                Key={"pk": {"S": lock_key(self.account_id)}},
                UpdateExpression="SET #exp = :new",
                ConditionExpression="#h = :me",
                ExpressionAttributeNames={"#exp": "lease_expires_at", "#h": "holder"},
                ExpressionAttributeValues={
                    ":me": {"S": self.owner},
                    ":new": {"N": str(now_ms() + LEASE_MS)},
                },
            )
            return True
        except self.ddb.exceptions.ConditionalCheckFailedException:
            self.lost = True
            return False

    def run(self) -> None:
        while not self.stop_event.wait(RENEW_INTERVAL_S):
            if not self.renew():
                return


def current_record(ddb, payload: Dict[str, Any]) -> Tuple[int, Dict[str, str]]:
    """The account request to apply: the item's current image if it exists,
    else the snapshot this invocation was triggered with."""
    account_request = payload["account_request"]
    fallback_version = int(account_request.get("record_version", 0))
    fallback_fields = json.loads(account_request.get("custom_fields", "{}"))
    try:
        resp = ddb.get_item(
            TableName=ACCOUNT_REQUEST_TABLE,
            Key={"id": {"S": account_request["id"]}},
            ConsistentRead=True,
        )
    except ddb.exceptions.ResourceNotFoundException:
        return fallback_version, fallback_fields
    item = resp.get("Item")
    if not item:
        return fallback_version, fallback_fields
    version = int(item["record_version"]["N"])
    fields = json.loads(item["custom_fields"]["S"])
    if version < fallback_version:
        return fallback_version, fallback_fields
    return version, fields


def applied_version(ddb, account_id: str) -> int:
    resp = ddb.get_item(
        TableName=SYNC_STATE_TABLE,
        Key={"pk": {"S": applied_key(account_id)}},
        ConsistentRead=True,
    )
    item = resp.get("Item") or {}
    return int(item.get("applied_version", {"N": "0"})["N"])


def advance_applied_version(ddb, account_id: str, version: int) -> None:
    try:
        ddb.update_item(
            TableName=SYNC_STATE_TABLE,
            Key={"pk": {"S": applied_key(account_id)}},
            UpdateExpression="SET #av = :v",
            ConditionExpression=(
                "attribute_not_exists(#av) OR #av < :v"
            ),
            ExpressionAttributeNames={"#av": "applied_version"},
            ExpressionAttributeValues={":v": {"N": str(version)}},
        )
    except ddb.exceptions.ConditionalCheckFailedException:
        pass  # a newer version was already recorded


def list_parameters(ssm) -> Dict[str, str]:
    paginator = ssm.get_paginator("get_parameters_by_path")
    pages = paginator.paginate(Path=SSM_PARAMETER_PATH, Recursive=True)
    existing: Dict[str, str] = {}
    for page in pages:
        for param in page["Parameters"]:
            existing[param["Name"]] = param["Value"]
    return existing


def apply_fields(ssm, existing: Dict[str, str], fields: Dict[str, str]) -> None:
    # Create or update every field the record carries before removing
    # anything, so a field the record keeps is never absent while it is
    # being refreshed.
    for key, value in fields.items():
        name = SSM_PARAMETER_PATH + key
        if existing.get(name) != value:
            ssm.put_parameter(Name=name, Value=value, Type="String", Overwrite=True)

    # Then remove parameters whose field the record no longer carries.
    stale: List[str] = [
        name for name in existing if name[len(SSM_PARAMETER_PATH):] not in fields
    ]
    for start in range(0, len(stale), DELETE_PARAMETER_BATCH_SIZE):
        batch = stale[start : start + DELETE_PARAMETER_BATCH_SIZE]
        ssm.delete_parameters(Names=batch)


def sync_custom_fields(event_payload: Dict[str, Any]) -> None:
    deadline = time.monotonic() + RUN_BUDGET_S
    account_id = event_payload["account_info"]["account"]["id"]

    ssm = service_client("ssm")
    ddb = service_client("dynamodb")

    owner = acquire_lease(ddb, account_id, deadline)
    if owner is None:
        raise TimeoutError(f"account {account_id} stayed busy past the run budget")
    heartbeat = LeaseHeartbeat(ddb, account_id, owner)
    heartbeat.start()
    try:
        version, fields = current_record(ddb, event_payload)
        if applied_version(ddb, account_id) >= version:
            # A record at least as new as the current one is already
            # applied; this invocation must not touch the parameters.
            return

        existing = list_parameters(ssm)
        if heartbeat.lost:
            raise RuntimeError(f"lease on {account_id} was taken over")
        apply_fields(ssm, existing, fields)
        if heartbeat.lost:
            raise RuntimeError(f"lease on {account_id} was taken over")
        advance_applied_version(ddb, account_id, version)
    finally:
        heartbeat.stop_event.set()
        release_lease(ddb, account_id, owner)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Apply an account request's custom fields to the member "
        "account's Parameter Store.",
        epilog="The payload format is documented in this module's docstring; "
        "an example is /app/payload-example.json.",
    )
    parser.add_argument("--payload", required=True, help="path to the payload JSON file")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format='{"module": "%(name)s", "log_level": "%(levelname)s", "log_message": "%(message)s"}',
        stream=sys.stderr,
    )

    try:
        with open(args.payload) as f:
            event = json.load(f)
        sync_custom_fields(event)
    except Exception as error:
        message = {
            "FILE": __file__.split("/")[-1],
            "METHOD": inspect.stack()[0][3],
            "EXCEPTION": str(error),
        }
        logger.exception(message)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
