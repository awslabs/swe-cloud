"""stackctl: deploy the app's stacks to the local engine.

    python3 -m stackctl bootstrap                  provision the account-level resources (once per environment)
    python3 -m stackctl deploy infra/rev-42        deploy every stack of a revision, in manifest order
    python3 -m stackctl deploy infra/rev-42 --exclusively Storefront
    python3 -m stackctl status                     stack statuses and physical ids
    python3 -m stackctl events [STACK] [-n N]      the engine's event log (requests, responses, statuses)
    python3 -m stackctl destroy STACK              delete a stack
    python3 -m stackctl reset                      forget all local stacks and wipe /cdk/exports + the tag table

State lives in $STACKCTL_HOME (default /var/lib/stackctl); handler stderr goes to $STACKCTL_HOME/logs and
published code assets to $STACKCTL_HOME/assets.
"""
import argparse
import json
import os
import shutil
import sys

import boto3
from botocore.exceptions import ClientError

from .engine import DEFAULT_TABLE, Engine, Sandbox

HOME = os.environ.get("STACKCTL_HOME", "/var/lib/stackctl")
STATE = os.path.join(HOME, "state.json")
EVENTS = os.path.join(HOME, "events.jsonl")
ASSETS = os.path.join(HOME, "assets")
TABLE = os.environ.get("CROSSREF_TABLE", DEFAULT_TABLE)
APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load():
    if os.path.exists(STATE):
        with open(STATE) as fh:
            return json.load(fh)
    return {}


def _save(stacks):
    os.makedirs(HOME, exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(stacks, fh, indent=1, sort_keys=True)
    os.replace(tmp, STATE)


def _printer(log):
    def observe(engine, event):
        with open(EVENTS, "a") as fh:
            fh.write(json.dumps(event) + "\n")
        _save(engine.stacks)
        if not log:
            return
        kind = event["kind"]
        if kind == "status":
            reason = f"  ({event['reason']})" if event.get("reason") else ""
            print(f"  {event['stack']:<16} {event['status']}{reason}")
        elif kind == "request":
            req = event["request"]
            print(f"  {event['stack']:<16}   -> {req['RequestType']:<6} {event['logical']}"
                  f" {req.get('PhysicalResourceId', '')}")
        elif kind == "response":
            resp = event["response"]
            reason = f"  {resp.get('Reason')}" if resp.get("Status") != "SUCCESS" else ""
            print(f"  {event['stack']:<16}   <- {resp.get('Status'):<7} {event['logical']}"
                  f" {resp.get('PhysicalResourceId', '')}{reason}")
            for stray in event.get("strays") or []:
                print(f"  {event['stack']:<16}      killed leftover handler process {stray['pid']}: {stray['cmdline']}")
        elif kind in ("refused", "abandoned", "no_changes"):
            print(f"  {event['stack']:<16} {kind}: {json.dumps({k: v for k, v in event.items() if k not in ('seq', 'time', 'kind', 'stack')})}")
    return observe


def _engine(log=True):
    os.makedirs(HOME, exist_ok=True)
    sandbox = Sandbox(app_dir=APP_DIR, table=TABLE, log_dir=os.path.join(HOME, "logs"))
    return Engine(stacks=_load(), sandbox=sandbox, observer=_printer(log), source_dir=APP_DIR, asset_dir=ASSETS)


def bootstrap(_args):
    ddb = boto3.client("dynamodb")
    try:
        ddb.create_table(
            TableName=TABLE,
            AttributeDefinitions=[{"AttributeName": "name", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "name", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
        print(f"created table {TABLE}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceInUseException":
            raise
        print(f"table {TABLE} already exists")
    return 0


def deploy(args):
    with open(os.path.join(args.revision, "manifest.json")) as fh:
        manifest = json.load(fh)
    engine = _engine()
    for entry in manifest["stacks"]:
        if args.exclusively and entry["name"] not in args.exclusively:
            continue
        with open(os.path.join(args.revision, entry["template"])) as fh:
            template = json.load(fh)
        print(f"{entry['name']}: deploying {args.revision}")
        status = engine.deploy(entry["name"], template)
        _save(engine.stacks)
        if status not in ("CREATE_COMPLETE", "UPDATE_COMPLETE"):
            print(f"{entry['name']}: {status}; stopping")
            return 1
    return 0


def status(_args):
    for name, stack in sorted(_load().items()):
        print(f"{name:<16} {stack['status']}  {stack.get('status_reason') or ''}")
        for logical, rec in stack["resources"].items():
            print(f"    {logical:<40} {rec['type']:<36} {rec['physical_id']}")
    return 0


def events(args):
    if not os.path.exists(EVENTS):
        return 0
    with open(EVENTS) as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    if args.stack:
        rows = [r for r in rows if r.get("stack") == args.stack]
    for row in rows[-args.n:]:
        print(json.dumps(row, sort_keys=True))
    return 0


def destroy(args):
    engine = _engine()
    result = engine.delete_stack(args.stack)
    _save(engine.stacks)
    return 0 if result == "DELETE_COMPLETE" else 1


def reset(_args):
    ssm = boto3.client("ssm")
    names = []
    for page in ssm.get_paginator("get_parameters_by_path").paginate(Path="/cdk/exports", Recursive=True):
        names += [p["Name"] for p in page["Parameters"]]
    for i in range(0, len(names), 10):
        ssm.delete_parameters(Names=names[i:i + 10])
    ddb = boto3.client("dynamodb")
    try:
        ddb.delete_table(TableName=TABLE)
        ddb.get_waiter("table_not_exists").wait(TableName=TABLE)
    except ClientError:
        pass
    for path in (STATE, EVENTS):
        if os.path.exists(path):
            os.unlink(path)
    shutil.rmtree(ASSETS, ignore_errors=True)
    print(f"deleted {len(names)} parameters, dropped {TABLE}, forgot all stacks and assets; run bootstrap again")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="stackctl", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("bootstrap").set_defaults(fn=bootstrap)
    p = sub.add_parser("deploy")
    p.add_argument("revision")
    p.add_argument("--exclusively", nargs="+", metavar="STACK")
    p.set_defaults(fn=deploy)
    sub.add_parser("status").set_defaults(fn=status)
    p = sub.add_parser("events")
    p.add_argument("stack", nargs="?")
    p.add_argument("-n", type=int, default=200)
    p.set_defaults(fn=events)
    p = sub.add_parser("destroy")
    p.add_argument("stack")
    p.set_defaults(fn=destroy)
    sub.add_parser("reset").set_defaults(fn=reset)
    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
