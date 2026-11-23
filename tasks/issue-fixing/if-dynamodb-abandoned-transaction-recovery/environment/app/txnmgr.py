"""Command entrypoint for the transaction program.

Roles:
  coordinator  begin | append | apply | commit | status | quit
  sweeper      sweep | run | stop | quit

One JSON object per line on stdin, one JSON object per response on
stdout. A sweeper that has been told to run passes over the transaction
table every sweep interval until it is told to stop or its input ends.
"""

import argparse
import json
import select
import sys

from db import Store
from protocol import (CompletedTransaction, ItemConflict, Protocol,
                      ProtocolError)
from sweeper import Sweeper

DEFAULT_ROLLBACK_AFTER_MS = 2000
DEFAULT_RETENTION_MS = 300000
DEFAULT_SWEEP_INTERVAL_MS = 100


def reply(obj):
    sys.stdout.write(json.dumps(obj, default=str) + "\n")
    sys.stdout.flush()


def parse(line):
    try:
        return json.loads(line)
    except ValueError:
        return None


def run_coordinator(proto, _args):
    for line in sys.stdin:
        command = parse(line)
        if command is None:
            reply({"ok": False, "error": "unparsable command"})
            continue
        name = command.get("cmd")
        try:
            if name == "begin":
                proto.begin(command["tx"])
                reply({"cmd": name, "tx": command["tx"], "ok": True})
            elif name == "append":
                rid = proto.append(command["tx"], command["item"], command["val"])
                reply({"cmd": name, "tx": command["tx"], "item": command["item"],
                       "rid": rid, "ok": True})
            elif name == "apply":
                rid = proto.apply(command["tx"], command["item"])
                reply({"cmd": name, "tx": command["tx"], "item": command["item"],
                       "rid": rid, "ok": True})
            elif name == "commit":
                outcome = proto.commit(command["tx"])
                outcome.update({"cmd": name, "ok": True})
                reply(outcome)
            elif name == "status":
                tx = proto.read_tx(command["tx"])
                reply({"cmd": name, "tx": command["tx"], "ok": True,
                       "state": tx["state"] if tx else None,
                       "version": int(tx["version"]) if tx else None,
                       "finalized": bool(tx.get("finalized")) if tx else False})
            elif name == "quit":
                reply({"cmd": name, "ok": True})
                return
            else:
                reply({"ok": False, "error": "unknown command %r" % name})
        except CompletedTransaction as err:
            reply({"cmd": name, "ok": False, "state": err.state, "error": str(err)})
        except ItemConflict as err:
            reply({"cmd": name, "ok": False, "item": err.item_id, "error": str(err)})
        except ProtocolError as err:
            reply({"cmd": name, "ok": False, "error": str(err)})
        except Exception as err:  # keep the loop alive for the next command
            reply({"cmd": name, "ok": False,
                   "error": "%s: %s" % (type(err).__name__, err)})


def run_sweeper(proto, args):
    sweeper = Sweeper(proto, args.rollback_after_ms, args.retention_ms)
    running = False
    while True:
        timeout = args.sweep_interval_ms / 1000.0 if running else None
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if ready:
            line = sys.stdin.readline()
            if not line:
                return
            command = parse(line)
            if command is None:
                reply({"ok": False, "error": "unparsable command"})
                continue
            name = command.get("cmd")
            try:
                if name == "sweep":
                    result = sweeper.sweep_tx(command["tx"])
                    result.update({"cmd": name, "ok": True})
                    reply(result)
                elif name == "run":
                    running = True
                    reply({"cmd": name, "ok": True})
                elif name == "stop":
                    running = False
                    reply({"cmd": name, "ok": True})
                elif name == "quit":
                    reply({"cmd": name, "ok": True})
                    return
                else:
                    reply({"ok": False, "error": "unknown command %r" % name})
            except Exception as err:
                reply({"cmd": name, "ok": False,
                       "error": "%s: %s" % (type(err).__name__, err)})
        elif running:
            sweeper.sweep_all()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Multi-item transaction coordinator and recovery sweeper")
    parser.add_argument("role", choices=["coordinator", "sweeper"])
    parser.add_argument("--endpoint", default=None)
    parser.add_argument("--region", default=None)
    parser.add_argument("--tx-table", required=True)
    parser.add_argument("--item-table", required=True)
    parser.add_argument("--image-table", required=True)
    parser.add_argument("--rollback-after-ms", type=int,
                        default=DEFAULT_ROLLBACK_AFTER_MS,
                        help="pending age beyond which recovery may intervene")
    parser.add_argument("--retention-ms", type=int, default=DEFAULT_RETENTION_MS,
                        help="how long completed records are kept before deletion")
    parser.add_argument("--sweep-interval-ms", type=int, default=DEFAULT_SWEEP_INTERVAL_MS)
    args = parser.parse_args(argv)

    store = Store(args.endpoint, args.region,
                  args.tx_table, args.item_table, args.image_table)
    store.ensure_tables()
    proto = Protocol(store)
    if args.role == "coordinator":
        run_coordinator(proto, args)
    else:
        run_sweeper(proto, args)


if __name__ == "__main__":
    main()
