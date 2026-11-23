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
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from lake.fileio import S3FileIO, retry_io
from lake.manifest import read_manifest, read_manifest_list
from lake.paths import BUCKET_DIR, EARLIEST, MANIFEST_DIR, manifest_path, snapshot_path
from lake.snapshot_manager import SnapshotManager

EXIT_CLEANED = 0
EXIT_USAGE = 2
EXIT_ABORTED = 3

# TABLE_FORMAT.md: once expiry has deleted any file of snapshot K, snapshot-K is gone within 2 s.
EXPIRY_MAX_DURATION_S = 2.0
EXPIRY_MARGIN_S = 0.5
EXPIRY_POLL_S = 0.1

LOG = logging.getLogger("orphan_clean")


class CleanAborted(Exception):
    """The used-file set cannot be determined; the clean must not delete anything."""


class OrphanCleaner:
    def __init__(self, fileio, older_than_ms, threads=8):
        self.fileio = fileio
        self.older_than_ms = older_than_ms
        self.threads = threads
        self.snapshot_manager = SnapshotManager(fileio)
        self.cache = {}
        self.cache_lock = threading.Lock()

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
        """Files used by the retained snapshots.

        A snapshot is retained exactly while its JSON exists, and expiry deletes a snapshot's
        manifests before its JSON, so a missing file of snapshot K is only explained once
        snapshot-K is gone (or EARLIEST has passed it). Expiry finishes within
        EXPIRY_MAX_DURATION_S of its first delete; a file still missing after that belongs to a
        retained snapshot and the used set cannot be determined.

        Commits only reference their parent's files or files newer than the cutoff, so a view is
        complete once its newest snapshot was read completely; newer snapshots add no candidate.
        """
        used = set()
        read = set()
        expired = set()
        while True:
            view = self.snapshot_manager.snapshot_ids()
            if not view:
                return used  # never committed: nothing is in use
            pending = [k for k in view if k not in read and k not in expired]
            with ThreadPoolExecutor(self.threads) as pool:
                results = list(pool.map(self.read_snapshot_files, pending))
            for snapshot_id, (files, missing) in zip(pending, results):
                used |= files  # partial reads of an expired snapshot only protect more
                if missing is None:
                    read.add(snapshot_id)
                elif self.await_expiry(snapshot_id):
                    LOG.info("snapshot %d expired while being read (%s)", snapshot_id, missing)
                    expired.add(snapshot_id)
                else:
                    raise CleanAborted(f"{missing} of retained snapshot {snapshot_id} is missing")
            if view[-1] in read:
                LOG.info("view %s complete, %d used files", view, len(used))
                return used
            # the newest snapshot of this view expired: newer snapshots exist, list again

    def read_snapshot_files(self, snapshot_id):
        """(files read, first missing path or None) for one snapshot."""
        files = set()
        try:
            snapshot = retry_io(lambda: self.snapshot_manager.snapshot(snapshot_id))
        except FileNotFoundError:
            return files, snapshot_path(snapshot_id)
        lists = [snapshot.base_manifest_list]
        if snapshot.delta_manifest_list:
            lists.append(snapshot.delta_manifest_list)
        for list_name in lists:
            files.add(manifest_path(list_name))
            try:
                manifests = self.cached(list_name, read_manifest_list)
            except FileNotFoundError:
                return files, manifest_path(list_name)
            for name in manifests:
                files.add(manifest_path(name))
                try:
                    files.update(self.cached(name, read_manifest))
                except FileNotFoundError:
                    return files, manifest_path(name)
        return files, None

    def cached(self, name, reader):
        """Manifest lists and manifests are immutable, so one read per run is enough."""
        with self.cache_lock:
            if name in self.cache:
                return self.cache[name]
        value = retry_io(lambda: reader(self.fileio, name))
        with self.cache_lock:
            self.cache[name] = value
        return value

    def await_expiry(self, snapshot_id):
        """True once expiry has removed the snapshot; False if it is still retained after the
        longest an expiry run can take."""
        deadline = time.monotonic() + EXPIRY_MAX_DURATION_S + EXPIRY_MARGIN_S
        while True:
            if not retry_io(lambda: self.snapshot_manager.snapshot_exists(snapshot_id)):
                return True
            earliest = self.read_earliest_hint()
            if earliest is not None and earliest > snapshot_id:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(EXPIRY_POLL_S)

    def read_earliest_hint(self):
        try:
            return int(retry_io(lambda: self.fileio.read_utf8(EARLIEST)).strip())
        except (FileNotFoundError, ValueError):
            return None

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
