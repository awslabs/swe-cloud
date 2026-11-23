# Archiver operations

## Restoring a job

The job manager restarts from the most recent retained checkpoint on its own. When that is not
possible, or when deploying a new version, the on-call restores by hand:

1. From the newest retained checkpoint: `--restore-from jobs/<job>/chk/<id>.json`.
2. If that one is unusable (bad deploy, poisoned records after it), from the older retained
   checkpoint.
3. For upgrades and migrations, from the savepoint taken beforehand
   (`jobs/<job>/savepoints/…`).

A restore by hand submits the job anew (locally: `jobmanager.py --restore-from <manifest key>`).
The new submission numbers its attempts from 0 again, as any submission does; checkpoint ids
carry on after the newest manifest present. We usually do it after the previous submission
failed or was cancelled, sometimes right after its last attempt was lost along with its host.

So every manifest that is present under `jobs/<job>/chk/` or `jobs/<job>/savepoints/` must stay
restorable for as long as it is present, as many times as it is used: its tail object exists
with exactly `tail_len` bytes, and its multipart upload is still open with the listed parts.
Deleting manifests is the job manager's business (retention) or the on-call's (savepoints); the
sink never touches `chk/` or `savepoints/`.

## Storage audit

Abandoned sink state costs money and confuses the publisher: in-progress multipart uploads are
billed for their parts until they are completed or aborted, and stray `_tmp_` objects next to the
part file were once picked up as data (INC-2291: "sink full of duplicated data", which is why the
sink cleans up after itself). The weekly audit, and the check after every incident, looks at each
job prefix once the job has stopped:

- every object under `jobs/<job>/`, other than the manifests under `chk/` and `savepoints/`, must
  be the `tail_key` of a manifest present under `chk/` or `savepoints/`;
- every in-progress multipart upload on a key under `jobs/<job>/` must be the `upload_id` of such
  a manifest;
- the bucket is not versioned; a deleted object must be gone, not hidden behind a delete marker.

Anything else is a finding against the job's owner. S3 lifecycle rules are not an option on the
archive bucket: savepoints and the live upload of a long-running job are routinely older than any
expiry we could set.
