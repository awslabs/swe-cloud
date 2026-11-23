# Vaultline backup worker

Run `python /app/worker.py --help`. For a local invocation:

```
python /app/catalog.py s3://backups/daily/ --endpoint-url http://localhost:4566
python /app/worker.py --dest s3://backups/daily/ --source /path/to/files --job-id daily --endpoint-url http://localhost:4566 --lease-ms 3000 --renew-ms 750
python /app/restore.py s3://backups/daily/ --endpoint-url http://localhost:4566
```

catalog.py provisions the bucket and a new destination. Do not run it again on an
existing destination. Sources are directory snapshots, recursively containing
ordinary files; names are relative POSIX paths. Worker invocations use fresh UUIDs,
including retries of the same job. stdout contains JSON: a `started` event before
storage access, then one `result` with that `backup_uuid` and a status:
BACKUP_CREATED (exit 0), BACKUP_ALREADY_EXISTS (exit 3), or FAILED (exit 1).
A failed invocation must terminate promptly rather than wait indefinitely.

The scheduler gives each worker a lease duration and a renewal period, in
milliseconds. Workers must refresh their reservation while running, even while
uploads are slow. Each refresh reserves the configured lease duration from the
time it is sent, not a shorter or longer substitute. An unfinished reservation
may be reused once its expires_at_ms has passed. It may not be reused sooner.
Jobs can run for many lease intervals. Processes share a synchronized UTC clock.
After a killed or unreachable worker stops refreshing, the retry policy allows
one lease duration plus 6 seconds for a replacement to complete a small backup.
Retry BACKUP_ALREADY_EXISTS with a fresh invocation. A published destination is
permanent: later invocations must report BACKUP_ALREADY_EXISTS.

Storage readers use `<prefix>/.lock`, a permanent JSON catalog record. Provisioning
sets state=free, epoch=0, owner=null, expires_at_ms=null, manifest_key=null.
Workers set state=held with owner=the attempt UUID, advance epoch on a new
reservation, and set expires_at_ms as UTC Unix milliseconds. Renewals retain the
owner and epoch. Publication changes state to committed and names manifest_key.
The record must remain present throughout its lifecycle. Only one backup can be
published at a destination. A BACKUP_CREATED result promises that this catalog
record identifies that attempt's complete backup, including after any other
workers finish. An old worker must not undo a newer reservation or publication.
No external coordination services are deployed: S3 is the durable store.

A manifest has `backup_uuid`, `job_id`, and `entries`. Each entry has `name`, `key`,
`size`, and hex `sha256`. restore.py follows only the catalog's manifest_key,
checks identity, and verifies every data object against the manifest. Keeping
unreferenced objects from interrupted runs is acceptable; garbage collection is
separate. Existing acknowledged data must survive late traffic from failed runs.

Workers may be killed at any point, or lose storage connectivity longer than their
lease. HTTP requests already sent can arrive later, and a connection can close
without delivering the response to an applied write. A successfully published
backup must still be reported as BACKUP_CREATED if the worker can establish that
its write was applied. Endpoints may be relays; never bypass the URL provided by
the scheduler. AWS credentials are test/test and region is us-east-1.

The emulator is Floci 2.2.0. Its S3 If-Match PUTs work on existing objects;
If-None-Match creates and guarded CopyObject are unavailable. A rejected
conditional PUT may spoil a persistent connection: close and replace the boto3
client after a 412 before further requests. This is a transport limitation, not
an additional ownership signal.
