"""Command line: ``syncctl sync|remove --config CONFIG.json``."""

from __future__ import annotations

import argparse
import json
import sys

from syncctl import config as config_mod
from syncctl import parameter_store, secrets_manager

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_CONFLICT = 3


def _destination(cfg):
    return parameter_store if cfg.destination == "ssm" else secrets_manager


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="syncctl", description=__doc__)
    parser.add_argument("command", choices=("sync", "remove"))
    parser.add_argument("--config", required=True, help="path to the sync's CONFIG.json")
    args = parser.parse_args(argv)

    try:
        cfg = config_mod.load(args.config)
        source = config_mod.load_source(cfg)
    except (OSError, ValueError) as exc:
        print(f"syncctl: {exc}", file=sys.stderr)
        return EXIT_USAGE

    report = {"created": [], "updated": [], "deleted": [], "conflicts": []}
    try:
        dest = _destination(cfg)
        if args.command == "sync":
            dest.sync(cfg, source, report)
        else:
            dest.remove(cfg, source, report)
    except Exception as exc:  # noqa: BLE001 - report and fail the run
        print(json.dumps(report))
        print(f"syncctl: {args.command} {cfg.sync_id} failed: {exc}", file=sys.stderr)
        return EXIT_FAILED

    print(json.dumps(report))
    return EXIT_CONFLICT if report["conflicts"] else EXIT_OK
