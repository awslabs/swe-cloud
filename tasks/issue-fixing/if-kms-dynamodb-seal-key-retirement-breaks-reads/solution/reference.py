import argparse
import base64
import json
import hashlib
import sys
import time

from botocore.exceptions import ClientError
from aws_store import ENTRIES, META, Store, encode
from envelope import Envelope


def retryable(error):
    return error.response["Error"]["Code"] in (
        "ConditionalCheckFailedException", "TransactionCanceledException",
        "TransactionConflictException")


def revision_condition(previous):
    if previous is None:
        return {"ConditionExpression": "attribute_not_exists(#id)",
                "ExpressionAttributeNames": {"#id": "id"}}
    return {"ConditionExpression": "#v = :old AND wrapped_dek = :wrap",
            "ExpressionAttributeNames": {"#v": "version"},
            "ExpressionAttributeValues": encode({":old": previous["version"],
                                                  ":wrap": previous["wrapped_dek"]})}


def commit(store, item, previous, route, receipt=None):
    check = {"TableName": META, "Key": encode({"pk": "routing"})}
    if route is None:
        check["ConditionExpression"] = "attribute_not_exists(pk)"
    else:
        check.update(ConditionExpression="target_arn = :target",
                     ExpressionAttributeValues=encode({":target": route["target_arn"]}))
    put = dict(TableName=ENTRIES, Item=encode(item), **revision_condition(previous))
    actions = [{"ConditionCheck": check}, {"Put": put}]
    if receipt is not None:
        actions.append({"Put": {"TableName": META, "Item": encode(receipt),
                               "ConditionExpression": "attribute_not_exists(pk)"}})
    store.ddb.transact_write_items(TransactItems=actions)


def adopt(store, previous):
    """Keep the one request the previous release recorded on its envelope."""
    receipt = dict(pk="request:" + hashlib.sha256(previous["last_req"].encode()).hexdigest(),
                   request_id=previous["last_req"], entry_id=previous["id"],
                   digest=previous["last_digest"])
    try:
        store.put(META, receipt, ConditionExpression="attribute_not_exists(pk)")
    except ClientError as error:
        if not retryable(error):
            raise


def writer(store, identity):
    for line in sys.stdin:
        request = json.loads(line)
        try:
            if request["op"] != "put":
                raise ValueError("unsupported operation")
            plaintext = base64.b64decode(request["value_b64"], validate=True)
            receipt = dict(pk="request:" + hashlib.sha256(request["req"].encode()).hexdigest(),
                           request_id=request["req"], entry_id=request["id"],
                           digest=hashlib.sha256(plaintext).hexdigest())
            while True:
                saved = store.read(META, {"pk": receipt["pk"]})
                if saved:
                    if any(saved.get(k) != receipt[k] for k in ("request_id", "entry_id", "digest")):
                        raise ValueError("request ID reused with different input")
                    break
                route = store.read(META, {"pk": "routing"})
                key = route["target_arn"] if route else store.config()["key_id"]
                previous = store.read(ENTRIES, {"id": request["id"]})
                if previous and previous.get("last_req"):
                    adopt(store, previous)
                    if previous["last_req"] == request["req"]:
                        continue
                try:
                    item = Envelope(store.kms, key).encrypt(plaintext)
                except ClientError:
                    # A wrap that reached KMS late may name a key retired meanwhile.
                    if store.read(META, {"pk": "routing"}) == route:
                        raise
                    continue
                item.update(id=request["id"], version=int(previous["version"]) + 1 if previous else 1)
                try:
                    commit(store, item, previous, route, receipt)
                    break
                except ClientError as error:
                    if not retryable(error):
                        raise
                    time.sleep(.01)
            reply = {"req": request["req"], "ok": True}
        except Exception as error:
            print(f"{identity}: {error}", file=sys.stderr, flush=True)
            reply = {"req": request["req"], "ok": False, "error": str(error)}
        print(json.dumps(reply), flush=True)


def get(store, identity):
    item = store.read(ENTRIES, {"id": identity})
    while True:
        if item is None:
            return 2
        # Reading depends on the blob's KMS key, not on the current write configuration.
        try:
            dek = store.kms.decrypt(CiphertextBlob=item["wrapped_dek"])["Plaintext"]
            break
        except ClientError:
            # The entry may have been rewrapped, and its former key retired, since it was read.
            latest = store.read(ENTRIES, {"id": identity})
            if latest is not None and latest["wrapped_dek"] == item["wrapped_dek"]:
                raise
            item = latest
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    print(base64.b64encode(AESGCM(dek).decrypt(item["iv"], item["ct"], None)).decode(), flush=True)
    return 0


def migrate(store):
    config = store.config()
    target = store.kms.describe_key(KeyId=config["key_id"])["KeyMetadata"]["Arn"]
    route = {"pk": "routing", "target_arn": target}
    # Publishing the routing decision before reading entries invalidates old requests
    # even when those requests were sent before this process started.
    store.put(META, route)
    store.put(META, dict(pk="migration", status="running", target_arn=target,
                         config_version=config["config_version"]))
    while True:
        mismatches = 0
        for previous in store.entries():
            if previous["key_arn"] == target:
                continue
            mismatches += 1
            result = store.kms.re_encrypt(CiphertextBlob=previous["wrapped_dek"],
                                          DestinationKeyId=target)
            item = dict(previous, key_arn=result["KeyId"], wrapped_dek=result["CiphertextBlob"])
            try:
                commit(store, item, previous, route)
            except ClientError as error:
                if not retryable(error):
                    raise
        if not mismatches:
            break
    store.put(META, dict(pk="migration", status="complete", target_arn=target,
                         config_version=config["config_version"]))
    print("seal migration complete", flush=True)


def main():
    parser = argparse.ArgumentParser(description="KMS envelope seal")
    commands = parser.add_subparsers(dest="command", required=True)
    writers = commands.add_parser("writer", help="serve JSON-line puts on stdin/stdout")
    writers.add_argument("--id", required=True)
    readers = commands.add_parser("get", help="read an entry as base64")
    readers.add_argument("id")
    commands.add_parser("migrate", help="migrate to the configured seal")
    args = parser.parse_args()
    store = Store()
    if args.command == "writer":
        writer(store, args.id)
    elif args.command == "get":
        return get(store, args.id)
    else:
        migrate(store)
    return 0


if __name__ == "__main__":
    sys.exit(main())
