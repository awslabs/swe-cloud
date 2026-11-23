#!/usr/bin/env python3
"""remove_orphan_files for one table.

Orphan files are data files and manifests that no snapshot references: leftovers of failed or
retried commits (a table's very first commit included) and of killed writers. Expiry is
otherwise the only thing that deletes files, so orphans accumulate until this job removes them.

The clean runs in three steps:
  1. list candidate files under bucket-0/ and manifest/ last modified before --older-than-ms
     (files written by in-flight commits are newer than that and are never candidates);
  2. collect the files used by the table's snapshots;
  3. delete candidates that are not used.

Output is one JSON line on stdout.
  exit 0  {"status": "CLEANED", "deleted": [<path>, ...]}
  exit 3  {"status": "ABORTED_INCOMPLETE", "reason": "..."}  the used files could not be
          determined; nothing under bucket-0/ or manifest/ was deleted
  exit 2  usage error
"""

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from lake.fileio import S3FileIO, retry_io
from lake.manifest import read_manifest, read_manifest_list
from lake.paths import BUCKET_DIR, MANIFEST_DIR, manifest_path
from lake.snapshot_manager import SnapshotManager

EXIT_CLEANED = 0
EXIT_USAGE = 2
EXIT_ABORTED = 3

LOG = logging.getLogger("orphan_clean")


class CleanAborted(Exception):
    """The used-file set cannot be determined; the clean must not delete anything."""


def retry_reading_files(reader, default):
    """Retry a read on IO errors. A file that does not exist was removed by a concurrent
    expiry, so the default is returned. When the retries run out the error propagates and the
    clean stops."""
    try:
        return retry_io(reader)
    except FileNotFoundError:
        return default


class OrphanCleaner:
    def __init__(self, fileio, older_than_ms, threads=8):
        self.fileio = fileio
        self.older_than_ms = older_than_ms
        self.threads = threads
        self.snapshot_manager = SnapshotManager(fileio)

    def clean(self):
        candidates = self.get_candidate_deleting_files()
        if not candidates:
            return []
        used = self.get_used_files()
        deletes = sorted(set(candidates) - used)
        with ThreadPoolExecutor(self.threads) as pool:
            list(pool.map(self.clean_file, deletes))
        return deletes

    def old_enough(self, status):
        return status.modification_time_ms < self.older_than_ms

    def get_candidate_deleting_files(self):
        """path -> size of every file under the data and manifest directories that is older
        than the cutoff. Listing failures are skipped: it is fine not to delete unread files."""
        candidates = {}
        for directory in (BUCKET_DIR, MANIFEST_DIR):
            try:
                statuses = retry_io(lambda: self.fileio.list_status(directory))
            except IOError as e:
                LOG.warning("failed to list %s, skip it: %s", directory, e)
                continue
            for status in statuses:
                if self.old_enough(status):
                    candidates[status.path] = status.size
        return candidates

    def get_used_files(self):
        used = set()
        manifests = set()
        snapshots = self.snapshot_manager.safely_get_all_snapshots()
        for snapshot in snapshots:
            self.collect_without_data_file(snapshot, used, manifests)

        def data_files(name):
            return retry_reading_files(lambda: read_manifest(self.fileio, name), [])

        with ThreadPoolExecutor(self.threads) as pool:
            for files in pool.map(data_files, sorted(manifests)):
                used.update(files)
        LOG.info("%d snapshots, %d manifests, %d used files", len(snapshots), len(manifests), len(used))
        return used

    def collect_without_data_file(self, snapshot, used, manifests):
        """Manifest lists and manifests of one snapshot (their data files are read later)."""
        lists = []
        if snapshot.delta_manifest_list:
            lists.append(snapshot.delta_manifest_list)
        lists.append(snapshot.base_manifest_list)
        for list_name in lists:
            used.add(manifest_path(list_name))
            for name in retry_reading_files(lambda: read_manifest_list(self.fileio, list_name), []):
                manifests.add(name)
                used.add(manifest_path(name))

    def clean_file(self, path):
        try:
            retry_io(lambda: self.fileio.delete(path))
        except IOError as e:
            LOG.warning("failed to delete %s: %s", path, e)


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Remove orphan files of one table.")
    parser.add_argument("--endpoint-url", required=True, help="S3 endpoint to use")
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--table-prefix", required=True, help="e.g. tables/events_raw/")
    parser.add_argument(
        "--older-than-ms",
        type=int,
        required=True,
        help="only files last modified before this epoch-millis cutoff are candidates",
    )
    parser.add_argument("--threads", type=int, default=8)
    return parser.parse_args(argv)


def main(argv=None):
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(name)s %(message)s")
    args = parse_args(argv)
    if args.older_than_ms >= int(time.time() * 1000):
        print("--older-than-ms must be in the past: files being written now would be deleted", file=sys.stderr)
        return EXIT_USAGE
    fileio = S3FileIO(args.bucket, args.table_prefix, endpoint_url=args.endpoint_url)
    cleaner = OrphanCleaner(fileio, args.older_than_ms, threads=args.threads)
    try:
        deleted = cleaner.clean()
    except (CleanAborted, IOError) as e:
        print(json.dumps({"status": "ABORTED_INCOMPLETE", "reason": str(e)}), flush=True)
        return EXIT_ABORTED
    print(json.dumps({"status": "CLEANED", "deleted": deleted}), flush=True)
    return EXIT_CLEANED


if __name__ == "__main__":
    sys.exit(main())
