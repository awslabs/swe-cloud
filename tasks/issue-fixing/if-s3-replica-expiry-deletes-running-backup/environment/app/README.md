# Full-backup repository service

This service stores full PostgreSQL-shaped backups in S3. It is a small Python
port of our repository maintenance path. There are no incremental dependencies,
WAL archives, encryption or compressed bundles in this deployment.

Start the local service with `bash /usr/local/bin/start_floci.sh` if needed. For
ordinary use:

```
python3 /app/repoctl.py init --bucket team-backups
python3 /app/repoctl.py backup --bucket team-backups --label 20250101-000000F
python3 /app/repoctl.py info --bucket team-backups
python3 /app/repoctl.py reconcile --bucket team-backups
python3 /app/repoctl.py verify --bucket team-backups
python3 /app/repoctl.py expire --bucket team-backups --retention-full 3
```

The bucket argument is arbitrary. The client uses `AWS_ENDPOINT_URL` (default
`http://localhost:4566`), region `us-east-1`, and local credentials `test`/`test`.
No account or region routing is involved. The primary in production is a separate
client of this format; maintenance cannot require changes to that client.

## Repository format 1

`backup/backup.info` is JSON:

```
{"format":1,"current":{"20250101-000000F":{"type":"full","prior":null,"files":8}}}
```

Each backup has its own `backup/YYYYMMDD-HHMMSSF/` prefix, containing
`backup.manifest.copy`, `backup.manifest`, and `pg_data/0000`, `pg_data/0001`, etc.
A final manifest is JSON with `label`, `type` (`full`), `prior` (`null`) and `files`
(number of data objects). A partial copy also has `copied`. Data bytes are opaque
to maintenance. Catalog entries and final manifests must remain compatible with
this format. Optional top-level catalog fields are permitted; the primary keeps
unknown fields when it registers a backup. Maintenance may also own objects under
`backup/.maintenance/`. Other objects outside backup label prefixes belong to
other tools.

The primary runs one backup at a time. A fresh backup's label sorts after all
existing labels. Resume is always enabled: the first object written in a fresh
prefix is its manifest copy, followed by data and periodic copy rewrites. To
finish, the primary writes the final copy, the final manifest, then registers the
backup in backup.info. The catalog registration is a conditional read-modify-write
with retry on a changed ETag. It uses no cross-host lock. A backup with a final
manifest is complete even if its catalog registration has not happened yet.
Missing complete entries are reconstructed by maintenance from their manifests,
except for backups already retired by a published maintenance decision.

Full retention is count based, newest first. Each maintenance invocation applies
the requested count to the complete backups it observes, and reclaims expired
storage. A backup completing during maintenance can count toward that invocation
or the next; it must remain complete in either case. Completed backups survive
until count retention selects them for expiration. Once maintenance publishes a
catalog without a previously listed backup, that retirement is final: leftover
manifests are cleanup work, not evidence to put it back. Increasing the retention
count later protects surviving backups; it does not undo published retirements.
Starting physical removal also commits a retirement. After an interruption,
recovery must finish that removal rather than roll it back.

Maintenance also reclaims unlisted debris from interrupted backups and partial
deletions in the same invocation. Only the newest label prefix in the repository
can still be resumed: when that prefix has a copy but no final manifest, it is
owned by the next backup's resume/cleanup path and maintenance must leave its
objects untouched, whether its primary is running or has crashed. Every
unfinished prefix older than the newest label is superseded and must be removed,
even if it still has its copy. A prefix with neither manifest is debris even
when it has the newest label. Object ages are
not an ownership signal: restores and migrations can create old labels with
fresh S3 timestamps. These rules let a maintenance run finish without waiting
for the primary.

## Catalog reconciliation

`python3 /app/repoctl.py reconcile --bucket team-backups` is the failover
controller's recovery command. It finishes storage removal already committed by
maintenance, reconstructs genuinely completed but unregistered backups, and
reclaims superseded unfinished prefixes and markerless debris. It makes no new
count-retention decisions and preserves the latest resumable work. It must work
on a fresh host using only repository state. Both reconcile and expire can run
again after any request was the last request their predecessor completed.
