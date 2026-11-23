"""Command line: `node serve` and `node restore`."""
import argparse
import json
import logging
import os
import sys

RESTORE_REFUSED = 3


def _setup_logging():
    level = os.environ.get("STRATA_LOG", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def cmd_serve(args):
    from .server import serve

    serve(args.db_dir, args.bucket, args.db, args.port)


def cmd_restore(args):
    from .replicator import Replicator, RestoreError

    if os.path.isdir(args.db_dir) and os.listdir(args.db_dir):
        print(f"--db-dir {args.db_dir} is not empty", file=sys.stderr)
        return 2
    replicator = Replicator(args.db_dir, args.bucket, args.db)
    try:
        head = replicator.latest_generation()
        replicator.restore(head)
    except RestoreError as e:
        print(f"restore refused: {e}", file=sys.stderr)
        return RESTORE_REFUSED
    seq, last_id, ids = replicator.db.state()
    print(json.dumps({"generation": head, "seq": seq, "head": last_id, "ids": ids}))
    return 0


def main(argv=None):
    _setup_logging()
    parser = argparse.ArgumentParser(prog="node")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve", help="run the writer node")
    p.add_argument("--db-dir", required=True)
    p.add_argument("--bucket", required=True)
    p.add_argument("--db", required=True)
    p.add_argument("--port", type=int, default=7000)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("restore", help="rebuild a database directory from the bucket")
    p.add_argument("--db-dir", required=True)
    p.add_argument("--bucket", required=True)
    p.add_argument("--db", required=True)
    p.set_defaults(func=cmd_restore)

    args = parser.parse_args(argv)
    return args.func(args) or 0
