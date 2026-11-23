# orphan cleanup deleted live data on events_raw

The nightly `remove_orphan_files` run on `tables/events_raw/` (2026-10-24, 02:00) reported
`CLEANED` and deleted files that the table still uses. Minutes later both the latest reader and a
time-travel reader failed with `NoSuchKey` on `bucket-0/` data files (see `/app/ops/scheduler.log`).
The ingestion jobs were committing and expiring snapshots the whole time, as they always are.

This is the second attempt at this cleaner. Build r412 aborted the run whenever the table changed
while it was cleaning. On `events_raw` that meant it aborted every night, orphans piled up until
storage alerted, and r412 was reverted. Whatever you change, the cleaner still has to finish and
reclaim orphans on tables that never stop committing. Waiting for the table to go quiet is not an
option.

What has to hold after your fix:

- A run must never delete a file that a retained snapshot uses.
- If the run cannot tell which files are in use, it deletes nothing and exits 3 with
  `ABORTED_INCOMPLETE`, as it already does for read failures.
- On a readable table, a run exits 0 within the scheduler's 60 s limit and removes every orphan
  older than `--older-than-ms`, even while commits and expiry keep landing.

The code is in `/app`: `orphan_clean.py` (the cleaner), `lake/` (the format library the ingestion
jobs share), `TABLE_FORMAT.md` (layout, commit and expiry semantics) and `README.md` (how the
scheduler runs the cleaner, and how to try it locally against the S3 emulator on
`localhost:4566`). Keep the command line, the output line and the exit codes. Each run is its own
process in a fresh working directory. It talks only to the `--endpoint-url` it is given, and its
role can list, read, stat and delete objects but cannot write. The ingestion side (`lake/commit.py`,
`lake/expire.py`) is not yours to change. Production committers run their own copy.
