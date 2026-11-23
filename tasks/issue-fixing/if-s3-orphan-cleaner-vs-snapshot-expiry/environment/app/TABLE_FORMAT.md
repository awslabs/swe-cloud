# Lake table format

Tables live in the `paimon-lake` bucket, one prefix per table (`tables/<name>/`). Every path
below is relative to the table prefix.

## Objects

| path | content | mutability |
|---|---|---|
| `snapshot/snapshot-<id>` | `{"id", "baseManifestList", "deltaManifestList", "commitKind", "timeMillis"}` | written once, deleted by expiry |
| `snapshot/LATEST` | decimal id of the newest snapshot | overwritten in place, may lag |
| `snapshot/EARLIEST` | decimal id of the oldest retained snapshot | overwritten in place, may lag |
| `manifest/manifest-list-<uuid>` | `{"manifests": ["manifest-<uuid>", ...]}` | immutable |
| `manifest/manifest-<uuid>` | `{"files": ["bucket-0/data-<uuid>.parquet", ...]}` | immutable |
| `bucket-0/data-<uuid>.parquet` | data | immutable |

The files of snapshot N are its two manifest lists, every manifest they name, and every data file
those manifests name. Object keys are never reused. The hints are only hints: `LATEST` is trusted
when `snapshot-(LATEST+1)` does not exist, `EARLIEST` when `snapshot-EARLIEST` exists; otherwise
list `snapshot/` (`lake/snapshot_manager.py`).

## Commits

One committer at a time per table (the ingestion jobs hold a commit lease). A commit writes its new
data files, a manifest for them, a new base list (the parent's base and delta manifests; when there
are many they are merged into one new manifest holding the same files) and a new delta list, then
`snapshot-<N+1>`, then `LATEST`. A full compaction rewrites every live file into new files and
starts a fresh base list. See `lake/commit.py`.

A commit only references files that its parent snapshot references or that the commit job
itself wrote after it started. There are no rollbacks, tags or branches on these tables.

## Snapshot retention and expiry

**A snapshot is retained exactly as long as `snapshot/snapshot-<id>` exists.** Every file of
every retained snapshot must stay readable: time-travel and incremental readers use snapshots
other than the latest.

Expiry (`lake/expire.py`, run by the committers) keeps the newest `snapshot.num-retained.max`
snapshots (at least `snapshot.num-retained.min`, never fewer than 1) and removes older ones from
the earliest end. Retention is configured per table by the job that owns it.

Expiry may lag behind commits: one run expires at most `snapshot.expire.limit` snapshots, and
several jobs run expiry asynchronously, off the commit path. A snapshot below the retention
window can therefore stay retained for a long time, and remains fully readable until it is
expired.

One expiry run deletes, in this order:

1. data files referenced by the expired snapshots and by no retained snapshot;
2. manifests and manifest lists of the expired snapshots that no retained snapshot references;
3. the `snapshot-<id>` files of the expired snapshots;
4. `EARLIEST`, set to the oldest retained id.

Each object is deleted with its own request. S3 has no multi-key atomicity, so while an expiry is
running a reader can find a snapshot file whose manifest lists or manifests are already gone.
Expiry runs under the committer's lease (`expire.max-duration` = 2 s): once a run has deleted any
file of snapshot K, `snapshot-K` is deleted within 2 seconds.

## Maintenance

`orphan_clean.py` removes files no retained snapshot references (see its docstring). The
maintenance role may list, read, stat and delete objects in the bucket; it cannot write.
