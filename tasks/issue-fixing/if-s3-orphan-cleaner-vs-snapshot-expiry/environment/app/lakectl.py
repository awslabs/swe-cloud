#!/usr/bin/env python3
"""Small operator tool for tables in the lake bucket (writer credentials; local use only).

  lakectl.py demo     --table-prefix tables/demo/        create a table with history and orphans
  lakectl.py commit   --table-prefix ... [--files N] [--merge-min-count M] [--compact]
  lakectl.py expire   --table-prefix ... --retain-max R [--retain-min 1] [--limit L]
  lakectl.py show     --table-prefix ...                 snapshots, hints and file counts

Endpoint and bucket default to $AWS_ENDPOINT_URL and paimon-lake.
"""

import argparse
import json
import sys
import time

from lake.commit import DEFAULT_MERGE_MIN_COUNT, commit_append, commit_compact
from lake.expire import expire
from lake.fileio import S3FileIO, make_client
from lake.manifest import new_data_file_path, read_manifest, read_manifest_list, write_manifest
from lake.paths import BUCKET_DIR, EARLIEST, LATEST, MANIFEST_DIR
from lake.snapshot_manager import SnapshotManager


def write_data_files(fileio, count):
    paths = [new_data_file_path() for _ in range(count)]
    for path in paths:
        fileio.write_bytes(path, b"PAR1" + path.encode() + b"PAR1")
    return paths


def cmd_demo(fileio, args):
    try:
        fileio.client.create_bucket(Bucket=fileio.bucket)
    except fileio.client.exceptions.BucketAlreadyOwnedByYou:
        pass
    except Exception as e:  # noqa: BLE001 - emulators differ on the "already exists" error
        if "BucketAlready" not in str(e):
            raise
    for _ in range(args.snapshots):
        commit_append(fileio, write_data_files(fileio, args.files), merge_min_count=1000)
    write_data_files(fileio, args.orphans)  # never committed: a writer killed before its commit
    write_manifest(fileio, [new_data_file_path()])  # an orphan manifest from a failed commit
    print(json.dumps({"table": fileio.prefix, "cutoff_ms": int(time.time() * 1000) + 1000}))
    print("wait a couple of seconds, then pass cutoff_ms as --older-than-ms", file=sys.stderr)


def cmd_commit(fileio, args):
    if args.compact:
        snapshot = commit_compact(fileio, write_data_files(fileio, max(args.files, 1)))
    else:
        snapshot = commit_append(fileio, write_data_files(fileio, args.files), args.merge_min_count)
    print(json.dumps(snapshot.to_json()))


def cmd_expire(fileio, args):
    count = expire(fileio, args.retain_max, args.retain_min, args.limit)
    print(json.dumps({"expired": count}))


def cmd_show(fileio, args):
    manager = SnapshotManager(fileio)
    hints = {}
    for name, path in (("LATEST", LATEST), ("EARLIEST", EARLIEST)):
        try:
            hints[name] = fileio.read_utf8(path).strip()
        except FileNotFoundError:
            hints[name] = None
    snapshots = []
    for snapshot in manager.safely_get_all_snapshots():
        lists = [snapshot.base_manifest_list] + ([snapshot.delta_manifest_list] if snapshot.delta_manifest_list else [])
        manifests = [m for name in lists for m in read_manifest_list(fileio, name)]
        files = {f for m in manifests for f in read_manifest(fileio, m)}
        snapshots.append({"id": snapshot.id, "kind": snapshot.commit_kind, "manifests": len(manifests), "files": len(files)})
    counts = {d: len(fileio.list_status(d)) for d in (BUCKET_DIR, MANIFEST_DIR)}
    print(json.dumps({"hints": hints, "snapshots": snapshots, "objects": counts}, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--endpoint-url", default=None)
    parser.add_argument("--bucket", default="paimon-lake")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo")
    demo.add_argument("--table-prefix", required=True)
    demo.add_argument("--snapshots", type=int, default=3)
    demo.add_argument("--files", type=int, default=5)
    demo.add_argument("--orphans", type=int, default=4)
    commit = sub.add_parser("commit")
    commit.add_argument("--table-prefix", required=True)
    commit.add_argument("--files", type=int, default=1)
    commit.add_argument("--merge-min-count", type=int, default=DEFAULT_MERGE_MIN_COUNT)
    commit.add_argument("--compact", action="store_true")
    exp = sub.add_parser("expire")
    exp.add_argument("--table-prefix", required=True)
    exp.add_argument("--retain-max", type=int, required=True)
    exp.add_argument("--retain-min", type=int, default=1)
    exp.add_argument("--limit", type=int, default=10)
    show = sub.add_parser("show")
    show.add_argument("--table-prefix", required=True)
    args = parser.parse_args(argv)
    fileio = S3FileIO(args.bucket, args.table_prefix, client=make_client(args.endpoint_url))
    {"demo": cmd_demo, "commit": cmd_commit, "expire": cmd_expire, "show": cmd_show}[args.command](fileio, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
