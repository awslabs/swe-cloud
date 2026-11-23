#!/usr/bin/env python3
"""Worker process for the table log store.

Reads one JSON command per line on stdin and answers one JSON line on stdout:

  {"op": "snapshot"}                              -> {"ok": true, "version": <int|null>}
  {"op": "commit", "version": N, "actions": "…"}  -> {"status": "committed"|"conflict"|"error", ...}
  {"op": "list_from", "start": N}                 -> {"ok": true, "entries": [[N, "…"], …]}

A "committed" answer means the version's log object holds exactly the caller's
actions and the coordination entry has been marked complete. Diagnostics go to
stderr.
"""

import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from logstore import ExternalLogStore
from storage import Storage


def handle(store, command):
    op = command.get("op")
    if op == "snapshot":
        return {"ok": True, "version": store.snapshot()}
    if op == "commit":
        version = int(command["version"])
        status = store.write(version, str(command["actions"]))
        return {"status": status, "version": version}
    if op == "list_from":
        entries = store.list_from(int(command["start"]))
        return {"ok": True, "entries": [[v, a] for v, a in entries]}
    return {"status": "error", "detail": "unknown op %r" % (op,)}


def main():
    store = ExternalLogStore(Storage.from_env())
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            command = json.loads(line)
        except ValueError as exc:
            reply = {"status": "error", "detail": "bad command: %s" % exc}
        else:
            try:
                reply = handle(store, command)
            except Exception as exc:
                traceback.print_exc()
                reply = {"status": "error", "detail": "%s: %s" % (type(exc).__name__, exc)}
        print(json.dumps(reply), flush=True)


if __name__ == "__main__":
    main()
