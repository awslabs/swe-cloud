import argparse
import base64
import json
import hashlib
import sys

from aws_store import ENTRIES, META, Store
from envelope import Envelope


def writer(store, identity):
    config = store.config()
    wrapper = Envelope(store.kms, config["key_id"])
    for line in sys.stdin:
        request = json.loads(line)
        try:
            if request["op"] != "put":
                raise ValueError("unsupported operation")
            previous = store.read(ENTRIES, {"id": request["id"]})
            value = base64.b64decode(request["value_b64"], validate=True)
            digest = hashlib.sha256(value).hexdigest()
            if previous and previous.get("last_req") == request["req"]:
                if previous.get("last_digest") != digest:
                    raise ValueError("request ID reused with different bytes")
                print(json.dumps({"req": request["req"], "ok": True}), flush=True)
                continue
            item = wrapper.encrypt(value)
            item.update(last_req=request["req"], last_digest=digest)
            item.update(id=request["id"], version=int(previous["version"]) + 1 if previous else 1)
            store.put(ENTRIES, item)
            reply = {"req": request["req"], "ok": True}
        except Exception as error:
            print(f"{identity}: {error}", file=sys.stderr, flush=True)
            reply = {"req": request["req"], "ok": False, "error": str(error)}
        print(json.dumps(reply), flush=True)


def get(store, identity):
    item = store.read(ENTRIES, {"id": identity})
    if item is None:
        return 2
    wrapper = Envelope(store.kms, store.config()["key_id"])
    print(base64.b64encode(wrapper.decrypt(item)).decode(), flush=True)
    return 0


def migrate(store):
    config = store.config()
    wrapper = Envelope(store.kms, config["key_id"])
    wrapper.encrypt(b"a")
    for item in store.entries():
        try:
            wrapper.decrypt(item)
            continue
        except Exception:
            pass
        dek = store.kms.decrypt(CiphertextBlob=item["wrapped_dek"])["Plaintext"]
        result = store.kms.encrypt(KeyId=config["key_id"], Plaintext=dek)
        item.update(key_arn=result["KeyId"], wrapped_dek=result["CiphertextBlob"])
        store.put(ENTRIES, item)
    store.put(META, dict(pk="migration", status="complete",
                         target_arn=wrapper.current_key_id,
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
