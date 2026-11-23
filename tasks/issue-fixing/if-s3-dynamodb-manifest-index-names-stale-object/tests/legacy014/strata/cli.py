"""`stratactl`: commit and resolve dataset versions.

    stratactl commit  --dataset D --version N --manifest-file F --committer NAME
    stratactl resolve --dataset D (--version N | --latest)

Both print one JSON line on stdout. Exit codes: 0 success, 1 unexpected error, 2 corrupt or
inconsistent manifest, 3 commit conflict (another writer holds the version), 4 not found.
"""

import argparse
import json
import logging
import os
import socket
import sys

from . import aws
from .errors import CommitConflict, StrataError
from .handler import ManifestCommitHandler
from .index import DynamoDBIndex
from .objects import ObjectStore


def _handler(committer_name):
    index = DynamoDBIndex(aws.dynamodb_client(), aws.table_name(), committer_name)
    objects = ObjectStore(aws.s3_client(), aws.bucket_name())
    return ManifestCommitHandler(index, objects)


def _reader_name():
    return os.environ.get("STRATA_CLIENT_ID") or f"reader-{socket.gethostname()}-{os.getpid()}"


def _emit(payload):
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    sys.stdout.flush()


def _location_payload(location):
    return {
        "version": location.version,
        "path": location.path,
        "size": location.size,
        "generation": location.generation,
    }


def cmd_commit(args):
    with open(args.manifest_file, "rb") as handle:
        manifest_bytes = handle.read()
    handler = _handler(args.committer)
    try:
        location = handler.commit(args.dataset, args.version, manifest_bytes)
    except CommitConflict as conflict:
        logging.getLogger("strata.cli").warning("%s", conflict)
        _emit({"status": "conflict", "version": args.version})
        return CommitConflict.exit_code
    _emit({"status": "committed", **_location_payload(location)})
    return 0


def cmd_resolve(args):
    handler = _handler(_reader_name())
    if args.latest:
        location = handler.resolve_latest_location(args.dataset)
    else:
        location = handler.resolve_version_location(args.dataset, args.version)
    _emit(_location_payload(location))
    return 0


def main(argv=None):
    logging.basicConfig(
        level=os.environ.get("STRATA_LOG_LEVEL", "WARNING"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    parser = argparse.ArgumentParser(prog="stratactl")
    sub = parser.add_subparsers(dest="command", required=True)

    commit = sub.add_parser("commit", help="commit a manifest as a new dataset version")
    commit.add_argument("--dataset", required=True)
    commit.add_argument("--version", required=True, type=int)
    commit.add_argument("--manifest-file", required=True)
    commit.add_argument("--committer", required=True)
    commit.set_defaults(func=cmd_commit)

    resolve = sub.add_parser("resolve", help="locate the manifest of a dataset version")
    resolve.add_argument("--dataset", required=True)
    which = resolve.add_mutually_exclusive_group(required=True)
    which.add_argument("--version", type=int)
    which.add_argument("--latest", action="store_true")
    resolve.set_defaults(func=cmd_resolve)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except StrataError as error:
        sys.stderr.write(f"error: {error}\n")
        return error.exit_code


if __name__ == "__main__":
    sys.exit(main())
