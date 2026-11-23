"""Snapshot discovery: hints, listing and loading."""

import time

from .paths import EARLIEST, LATEST, SNAPSHOT_DIR, snapshot_id_of, snapshot_path
from .snapshot import Snapshot

READ_HINT_RETRY_NUM = 3
READ_HINT_RETRY_INTERVAL_S = 0.001


class SnapshotManager:
    def __init__(self, fileio):
        self.fileio = fileio

    # ---- loading -------------------------------------------------------------------------

    def snapshot(self, snapshot_id):
        """Load snapshot-<id>. FileNotFoundError if its JSON does not exist."""
        return Snapshot.from_json(self.fileio.read_json(snapshot_path(snapshot_id)))

    def snapshot_exists(self, snapshot_id):
        return self.fileio.exists(snapshot_path(snapshot_id))

    def snapshot_ids(self):
        """Ids of the snapshot JSON objects currently under snapshot/, ascending."""
        ids = []
        for status in self.fileio.list_status(SNAPSHOT_DIR):
            snapshot_id = snapshot_id_of(status.path)
            if snapshot_id is not None:
                ids.append(snapshot_id)
        return sorted(ids)

    def safely_get_all_snapshots(self):
        """Every snapshot under snapshot/; ones that disappear while being read are skipped."""
        snapshots = []
        for snapshot_id in self.snapshot_ids():
            try:
                snapshots.append(self.snapshot(snapshot_id))
            except FileNotFoundError:
                pass
        return snapshots

    # ---- hints -----------------------------------------------------------------------------

    def _read_hint(self, path):
        for _ in range(READ_HINT_RETRY_NUM):
            try:
                return int(self.fileio.read_utf8(path).strip())
            except Exception:
                pass
            time.sleep(READ_HINT_RETRY_INTERVAL_S)
        return None

    def latest_snapshot_id(self):
        """LATEST is trusted only when there is no snapshot after it; otherwise list."""
        hint = self._read_hint(LATEST)
        if hint is not None and hint > 0 and not self.snapshot_exists(hint + 1):
            return hint
        ids = self.snapshot_ids()
        return ids[-1] if ids else None

    def earliest_snapshot_id(self):
        """EARLIEST is trusted only when that snapshot exists; otherwise list."""
        hint = self._read_hint(EARLIEST)
        if hint is not None and self.snapshot_exists(hint):
            return hint
        ids = self.snapshot_ids()
        return ids[0] if ids else None

    def commit_latest_hint(self, snapshot_id):
        self.fileio.write_utf8(LATEST, str(snapshot_id))

    def commit_earliest_hint(self, snapshot_id):
        self.fileio.write_utf8(EARLIEST, str(snapshot_id))

    # ---- readers ---------------------------------------------------------------------------

    def latest_snapshot(self):
        """The latest snapshot. A latest that vanishes while being read was expired by a
        concurrent commit, so resolve the latest again; if it did not move, the snapshot is
        genuinely missing and the error is raised."""
        snapshot_id = self.latest_snapshot_id()
        while snapshot_id is not None:
            try:
                return self.snapshot(snapshot_id)
            except FileNotFoundError:
                new_id = self.latest_snapshot_id()
                if new_id == snapshot_id:
                    return self.snapshot(snapshot_id)
                snapshot_id = new_id
        return None

    def traverse_from_latest_safely(self, predicate):
        """Newest snapshot satisfying `predicate`, walking back to the earliest. A snapshot that
        fails to load below the (re-read) earliest id was expired; at or above it, it is a valid
        snapshot and the error is raised."""
        latest_id = self.latest_snapshot_id()
        if latest_id is None:
            return None
        earliest_id = self.earliest_snapshot_id()
        if earliest_id is None:
            return None
        for snapshot_id in range(latest_id, earliest_id - 1, -1):
            try:
                snapshot = self.snapshot(snapshot_id)
            except Exception:
                new_earliest = self.earliest_snapshot_id()
                if new_earliest is None:
                    return None
                if snapshot_id >= new_earliest:
                    raise
                return None
            if predicate(snapshot):
                return snapshot
        return None
