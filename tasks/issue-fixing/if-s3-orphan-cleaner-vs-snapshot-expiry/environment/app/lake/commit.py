"""Writer side: how the ingestion jobs commit a snapshot (one committer per table at a time)."""

import time

from .manifest import read_manifest, read_manifest_list, write_manifest, write_manifest_list
from .paths import snapshot_path
from .snapshot import Snapshot
from .snapshot_manager import SnapshotManager

DEFAULT_MERGE_MIN_COUNT = 30


def _write_snapshot(fileio, manager, snapshot_id, base, delta, kind):
    snapshot = Snapshot(
        id=snapshot_id,
        base_manifest_list=write_manifest_list(fileio, base),
        delta_manifest_list=write_manifest_list(fileio, delta),
        commit_kind=kind,
        time_millis=int(time.time() * 1000),
    )
    fileio.write_json(snapshot_path(snapshot_id), snapshot.to_json())
    manager.commit_latest_hint(snapshot_id)
    if snapshot_id == 1:
        manager.commit_earliest_hint(1)
    return snapshot


def commit_append(fileio, new_files, merge_min_count=DEFAULT_MERGE_MIN_COUNT):
    """Append `new_files` (already written to bucket-0/). The parent's base and delta manifests
    become the new base; once there are at least `merge_min_count` of them they are merged into
    one new manifest holding all their files (the old manifests are then no longer referenced by
    the new snapshot, while their data files still are)."""
    manager = SnapshotManager(fileio)
    parent_id = manager.latest_snapshot_id()
    base = []
    if parent_id is not None:
        parent = manager.snapshot(parent_id)
        base = read_manifest_list(fileio, parent.base_manifest_list)
        if parent.delta_manifest_list:
            base += read_manifest_list(fileio, parent.delta_manifest_list)
    if base and len(base) >= merge_min_count:
        merged = []
        for name in base:
            merged.extend(read_manifest(fileio, name))
        base = [write_manifest(fileio, merged)]
    delta = [write_manifest(fileio, new_files)] if new_files else []
    return _write_snapshot(fileio, manager, (parent_id or 0) + 1, base, delta, "APPEND")


def commit_compact(fileio, rewritten_files):
    """Full compaction: every live file was rewritten into `rewritten_files`."""
    manager = SnapshotManager(fileio)
    parent_id = manager.latest_snapshot_id()
    base = [write_manifest(fileio, rewritten_files)]
    return _write_snapshot(fileio, manager, (parent_id or 0) + 1, base, [], "COMPACT")
