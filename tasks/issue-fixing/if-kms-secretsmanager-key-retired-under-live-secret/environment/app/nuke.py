#!/usr/bin/env python3
"""Sandbox sweep: remove every resource of the configured types that no filter keeps.

    python3 /app/nuke.py run --config CONFIG.json [--no-dry-run]

Without --no-dry-run the scan is printed and nothing is changed.

Exit codes:
    0  every item the sweep set out to remove is gone (or, in a dry run, the scan finished)
    2  bad usage or config
    3  the sweep stopped with items it could not remove (failed or held); the next sweep retries them
    any other: the sweep crashed
"""
import argparse
import json
import sys

import resources  # noqa: F401  (registers the resource types)
from libnuke.engine import COMPLETE, Nuke
from libnuke.filters import Filters
from libnuke.queue import ItemState

EXIT_COMPLETE = 0
EXIT_USAGE = 2
EXIT_INCOMPLETE = 3


def load_config(path):
    with open(path) as fh:
        config = json.load(fh)
    settings = {}
    for type_name, values in (config.get("settings") or {}).items():
        settings[type_name] = dict(values)
    for type_name in config.get("resource_types") or []:
        settings.setdefault(type_name, {})
    if config.get("backup_bucket"):
        for values in settings.values():
            values.setdefault("backup_bucket", config["backup_bucket"])
    return config, settings


def main(argv=None):
    parser = argparse.ArgumentParser(prog="nuke.py")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="scan and (with --no-dry-run) remove")
    run.add_argument("--config", required=True)
    run.add_argument("--no-dry-run", action="store_true")
    args = parser.parse_args(argv)

    try:
        config, settings = load_config(args.config)
        filters = Filters(config.get("filters"))
        filters.validate()
    except (OSError, ValueError, KeyError) as err:
        print(f"config error: {err}", file=sys.stderr)
        return EXIT_USAGE

    flags = config.get("feature_flags") or {}
    nuke = Nuke(
        filters=filters,
        settings=settings,
        pass_sleep_s=float(config.get("pass_sleep_s", 5)),
        wait_on_dependencies=bool(flags.get("wait_on_dependencies", False)),
        resource_types=config.get("resource_types"),
    )

    print("starting scan for resources", flush=True)
    nuke.scan()
    if not args.no_dry_run:
        print("The above resources would be deleted with the supplied configuration. "
              "Provide --no-dry-run to actually destroy resources.", flush=True)
        return EXIT_COMPLETE

    outcome = nuke.run()
    q = nuke.queue
    print(f"Nuke complete: {q.count(ItemState.FAILED)} failed, {q.count(ItemState.FILTERED)} skipped, "
          f"{q.count(ItemState.FINISHED)} finished.", flush=True)
    return EXIT_COMPLETE if outcome == COMPLETE else EXIT_INCOMPLETE


if __name__ == "__main__":
    sys.exit(main())
