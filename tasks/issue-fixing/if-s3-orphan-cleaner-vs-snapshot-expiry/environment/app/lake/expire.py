"""Snapshot expiry as the committers run it after a commit.

Options (per table, set by the owning ingestion job):
  snapshot.num-retained.max  keep at most this many newest snapshots
  snapshot.num-retained.min  always keep at least this many (>= 1)
  snapshot.expire.limit      expire at most this many snapshots per run
"""

import logging

from .manifest import read_manifest, read_manifest_list
from .paths import BUCKET_DIR, manifest_path, snapshot_path
from .snapshot_manager import SnapshotManager

LOG = logging.getLogger("lake.expire")


def _files_of(fileio, snapshot):
    """(manifest lists, manifests, data files) referenced by one snapshot."""
    lists = [snapshot.base_manifest_list]
    if snapshot.delta_manifest_list:
        lists.append(snapshot.delta_manifest_list)
    manifests, data = set(), set()
    for name in lists:
        for manifest in read_manifest_list(fileio, name):
            manifests.add(manifest)
            data.update(read_manifest(fileio, manifest))
    return set(lists), manifests, data


def expire(fileio, retain_max, retain_min=1, expire_limit=10):
    if retain_min < 1 or retain_max < retain_min:
        raise ValueError("need 1 <= retain_min <= retain_max")
    manager = SnapshotManager(fileio)
    latest = manager.latest_snapshot_id()
    earliest = manager.earliest_snapshot_id()
    if latest is None or earliest is None:
        return 0
    end_exclusive = min(
        max(latest - retain_max + 1, earliest),
        latest - retain_min + 1,
        earliest + expire_limit,
    )
    return expire_until(fileio, manager, earliest, end_exclusive, latest)


def expire_until(fileio, manager, earliest, end_exclusive, latest):
    if end_exclusive <= earliest:
        return 0
    expired, retained = [], []
    for snapshot_id in range(earliest, latest + 1):
        try:
            snapshot = manager.snapshot(snapshot_id)
        except FileNotFoundError:
            continue  # already expired by another committer
        (expired if snapshot_id < end_exclusive else retained).append(snapshot)
    if not expired:
        return 0

    keep_manifests, keep_data = set(), set()
    for snapshot in retained:
        _, manifests, data = _files_of(fileio, snapshot)
        keep_manifests |= manifests
        keep_data |= data
    drop_lists, drop_manifests, drop_data = set(), set(), set()
    for snapshot in expired:
        lists, manifests, data = _files_of(fileio, snapshot)
        drop_lists |= lists
        drop_manifests |= manifests
        drop_data |= data

    # data files first, then manifests and manifest lists, then the snapshot files, then the hint
    for path in sorted(drop_data - keep_data):
        if path.startswith(BUCKET_DIR):
            fileio.delete(path)
    for name in sorted(drop_manifests - keep_manifests):
        fileio.delete(manifest_path(name))
    for name in sorted(drop_lists):
        fileio.delete(manifest_path(name))
    for snapshot in expired:
        fileio.delete(snapshot_path(snapshot.id))
    manager.commit_earliest_hint(end_exclusive)
    LOG.info("expired snapshots [%d, %d)", earliest, end_exclusive)
    return len(expired)
