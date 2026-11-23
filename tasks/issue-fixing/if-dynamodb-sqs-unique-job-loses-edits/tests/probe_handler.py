"""REFRESH_CMD used by the verifier: records each start, optionally waits at a barrier, then
records the dirty_seq it saw as processed_seq. Imports nothing from /app."""

import argparse
import json
import sys
import time

import boto3


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--entity", required=True)
    parser.add_argument("--message-id", required=True)
    parser.add_argument("--attempt", default="")
    args, _ = parser.parse_known_args()
    with open(args.config) as handle:
        cfg = json.load(handle)
    ddb = boto3.client("dynamodb", endpoint_url=cfg["endpoint"], region_name=cfg["region"])
    key = {"pk": {"S": args.entity}}

    ordinal = int(ddb.update_item(
        TableName=cfg["entity_table"], Key=key, UpdateExpression="ADD starts :one",
        ExpressionAttributeValues={":one": {"N": "1"}},
        ReturnValues="UPDATED_NEW")["Attributes"]["starts"]["N"])
    item = ddb.get_item(TableName=cfg["entity_table"], Key=key, ConsistentRead=True)["Item"]
    seen = int(item.get("dirty_seq", {}).get("N", "0"))

    def ledger(event, **extra):
        ddb.put_item(TableName=cfg["ledger_table"], Item={
            "pk": {"S": args.entity}, "sk": {"S": f"{ordinal:04d}#{event}"},
            "message_id": {"S": args.message_id}, "attempt": {"S": str(args.attempt)},
            "seen": {"N": str(seen)}, "t": {"N": repr(time.time())},
            **{k: {"S": str(v)} for k, v in extra.items()}})

    ledger("start")
    fail = False
    barrier_key = {"pk": {"S": f"{args.entity}#{ordinal}"}}
    barrier = ddb.get_item(TableName=cfg["barrier_table"], Key=barrier_key,
                           ConsistentRead=True).get("Item")
    if barrier:
        fail = barrier.get("fail_run", {}).get("BOOL", False)
        deadline = time.time() + cfg["barrier_timeout"]
        while time.time() < deadline:
            barrier = ddb.get_item(TableName=cfg["barrier_table"], Key=barrier_key,
                                   ConsistentRead=True).get("Item", {})
            if barrier.get("gate_open", {}).get("BOOL", False):
                break
            time.sleep(0.1)
        else:
            ledger("end", outcome="barrier_timeout")
            return 3
    time.sleep(cfg.get("work_seconds", 0.3))
    if fail:
        ledger("end", outcome="fail")
        return 1
    ddb.update_item(TableName=cfg["entity_table"], Key=key,
                    UpdateExpression="SET processed_seq = :seen",
                    ExpressionAttributeValues={":seen": {"N": str(seen)}})
    ledger("end", outcome="ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
