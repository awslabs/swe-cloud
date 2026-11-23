"""Command line interface.

    chambersm write   <service> <key> <value>
    chambersm read    <service> <key> [--version N]
    chambersm history <service> <key>
    chambersm delete  <service> <key>
    chambersm list    <service> [--values]
    chambersm export  <service>

``write`` exits 0 once the value is stored; any other exit status means the write was not
acknowledged. ``read`` prints the bare value. ``history`` prints one JSON object per line,
oldest version first: {"event", "version", "value", "created", "created_by"}. ``list`` and
``export`` print JSON.
"""

import argparse
import json
import re
import sys

from botocore.exceptions import BotoCoreError, ClientError

from chambersm.store import SecretNotFound, SecretsManagerStore, StoreError

_SERVICE_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _service(value):
    if not _SERVICE_RE.match(value):
        raise argparse.ArgumentTypeError(f"invalid service name: {value!r}")
    return value


def _key(value):
    if not _KEY_RE.match(value) or value.startswith("_"):
        raise argparse.ArgumentTypeError(f"invalid key: {value!r}")
    return value


def build_parser():
    parser = argparse.ArgumentParser(prog="chambersm", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("write", help="write a value")
    p.add_argument("service", type=_service)
    p.add_argument("key", type=_key)
    p.add_argument("value")

    p = sub.add_parser("read", help="print a value")
    p.add_argument("service", type=_service)
    p.add_argument("key", type=_key)
    p.add_argument("--version", type=int, default=-1)

    p = sub.add_parser("history", help="print the versions of a key")
    p.add_argument("service", type=_service)
    p.add_argument("key", type=_key)

    p = sub.add_parser("delete", help="remove a key")
    p.add_argument("service", type=_service)
    p.add_argument("key", type=_key)

    p = sub.add_parser("list", help="list the keys of a service")
    p.add_argument("service", type=_service)
    p.add_argument("--values", action="store_true")

    p = sub.add_parser("export", help="print the current key/value map")
    p.add_argument("service", type=_service)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    store = SecretsManagerStore()
    try:
        if args.command == "write":
            if args.value == "":
                raise StoreError("refusing to write an empty value; use delete")
            store.write(args.service, args.key, args.value)
        elif args.command == "read":
            print(store.read(args.service, args.key, args.version)["value"])
        elif args.command == "history":
            for event in store.history(args.service, args.key):
                print(json.dumps(event))
        elif args.command == "delete":
            store.delete(args.service, args.key)
        elif args.command == "list":
            print(json.dumps(store.list(args.service, include_values=args.values), indent=2))
        elif args.command == "export":
            print(json.dumps(store.list_raw(args.service), indent=2, sort_keys=True))
    except SecretNotFound as err:
        print(f"chambersm: not found: {err}", file=sys.stderr)
        return 2
    except StoreError as err:
        print(f"chambersm: {err}", file=sys.stderr)
        return 1
    except (ClientError, BotoCoreError) as err:
        print(f"chambersm: {err}", file=sys.stderr)
        return 1
    return 0
