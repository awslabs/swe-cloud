# Metrics materializer

This is a local-process port of our continuous transform service. It uses an AWS OpenSearch 2.11
domain (OpenSearch engine 2.11.1). The scheduler starts a fresh Python process per invocation;
a replacement can overlap an abandoned predecessor. Runtime state belongs in OpenSearch. A process
may have a new working directory and no local files from its predecessor.

Run from any directory, with PYTHONPATH=/app:

    python -m transform_runner run_once --now-ms 1745194500000

Exit 0 means the invocation committed. Exit 75 asks the scheduler for another invocation; other
nonzero exits indicate failure. Retries can receive the same clock value or a later one.
The scheduler supplies epoch milliseconds, rather than relying on engine date math. Intervals may
be delayed or skipped. Clock values are nondecreasing in dispatch order, including retries; an
abandoned process retains its original clock when it resumes.

Environment variables: OS_ENDPOINT (HTTP endpoint), SOURCE_INDEX (default metrics-src),
TARGET_INDEX (metrics-hourly), META_INDEX (transform-meta), TRANSFORM_ID (hourly).
`transform.json` is deployment-owned configuration; do not change it to resolve this incident.
`admin.py provision --domain <unique-name> --now-ms 1745193600000` creates a development domain
and empty indices, commits the empty backfill at that clock and prints its endpoint. There is no scheduler/plugin installation to debug.

## Data and output

Source fields are ts (epoch-millisecond date), device (keyword), m1 (integer). Three primary
shards, no replicas. Ingestion may deliver old events or overwrite m1/voided on an existing document.
`voided: true` revokes an event; missing or false means active. Revocations are retained source
records and can later be restored by setting voided false. ts/device never change. Archival
retention may physically delete events once their ts is at or below the current selection's
lower boundary, before the worker is dispatched. Selected events are never physically deleted. Shard counters need not resemble one another.
Source refreshes are explicit; ingestion is visible and primary global checkpoints have settled before
an invocation starts. Ingestion may continue while the worker runs.

The dashboard is a rolling 48-hour view with a one-hour holdback. Let U = now_ms - 60 minutes:
select active documents with U - 48 hours < ts <= U. `window_minutes` in transform.json is 2880.
Both timestamp boundaries are exact milliseconds; neither is rounded to an hour. The archived
source and target can contain older data. Source archival and worker execution are independent.
Group by UTC fixed one-hour ts buckets and device. Even the hour containing the selection edge
is materialized: it is a partial hour, not an instruction to round the selection down.
Each target document has id `<transform_id>#<bucket_start_ms>:<device>` and fields
bucket_start_ms, device, doc_count, m1_sum, m1_count, m1_avg. m1_count counts present m1 values;
average is null when no values exist. A successful run must converge to this view. Writes arriving
during a run may be included then or next time, but cannot be forgotten. Stable ids make replay
safe. Existing backfilled buckets still inside the selection must remain correct.

The dashboard hides rows with doc_count 0. When a previously materialized key has no selected
events, it must have no visible row: an absent document or a row with doc_count=0, m1_sum=0,
m1_count=0, m1_avg=null is acceptable. A zero row retains bucket_start_ms/device and its stable
ID; extra fields have no prescribed meaning. The gateway offers overwrites rather than physical
target deletes. A live event with no m1 still contributes to doc_count. Revoking the last event
and expiring a whole bucket must remove its contribution, including after interrupted delivery.
Restoring a retained event must restore its contribution. No retired-bucket catalogue is seeded
in metadata. The target itself is readable, including buckets whose source events were archived.

## Progress record

`META_INDEX/_doc/TRANSFORM_ID` is the durable progress record. Existing installations already have
one, written at the end of backfill. It contains:

- shard_checkpoints: object mapping each primary shard id (string) to its committed global
  checkpoint. This identifies the run-start snapshot covered by a completed invocation, not the
  newest counter visible at the time of the final metadata write.
- selection_upper_bound_ms: inclusive upper timestamp boundary represented by the committed selection;
  the exclusive lower boundary is this value minus the configured window length in milliseconds. Both edges describe the same view.
- status: `committed` after a completed invocation.

The committed record is a recovery promise. An interrupted invocation may have overwritten some
bucket documents, but must leave enough uncommitted progress for its replacement to finish. Never
advance committed progress past unwritten materialization. Extra metadata fields are acceptable;
no auxiliary indices or external persistence are provisioned.

## Operating tier

Initial backfill is an administrative operation, not part of run_once. Incremental invocations
have a 3,000-document read allowance and 120 seconds to finish, even on a large retained source.
The read allowance is the sum of exact matched-document counts for every search request (or count
request), including every composite page; size:0 still reads the aggregation's matching documents.
Avoid tiny pages when many buckets are dirty. The same service can face a two-hour scheduler gap.
Source retention may already have removed events that expired during that gap.
Reads and writes must use OS_ENDPOINT; see gateway.md. The source and deployment config are read
only. Target writes overwrite materializations; the worker owns only its transform's metadata.


Target capacity is shared with other tenants. Each invocation has credit for 8 target document
writes, including overwrites of identical values. Once that credit is spent, the gateway rejects
further target items until a new invocation. It does not refill while the process waits. Metadata
writes and refresh requests do not spend target credit. Bulk requests can partly succeed: an HTTP
200 may contain successful items and rejected items, in the original request order. See gateway.md.
The service must make progress across fresh processes under sustained backpressure, and finish
within the scheduler's 64-invocation retry horizon for one dispatch. The source may change and the
clock may advance between attempts. Exit 0 promises the view for that invocation's supplied clock;
finishing only an older attempt is insufficient. Unfinished work may be stored in the existing
metadata record; extra fields have no prescribed schema. An interrupted worker can have accepted
some target items without receiving their responses.


## Replacement ownership

The scheduler can abandon a worker whose requests have stopped responding and immediately dispatch
its replacement. The old process is not guaranteed to stop. Its already-issued HTTP requests can
remain queued and reach OpenSearch after the replacement has committed, even if the old connection
has closed. At most one non-abandoned invocation is dispatched at a time. A replacement must make
progress without waiting for an abandoned process or its requests to finish. The normal retry
horizon and per-invocation allowances still apply.

An abandoned process must never undo newer materialization or erase the replacement's progress.
This applies to both target documents and the shared metadata record. Stable target IDs avoid
creating duplicates but alone do not order replacements computed from different source states.
Recovery must preserve unfinished work when ownership changes. Extra metadata fields and target
fields are allowed; existing metric fields and IDs retain their meanings. A superseded invocation
may exit 75, or exit 0 if its work is already covered by a newer completed invocation. Operations
must not need another scheduled run to repair damage caused by a late predecessor request.

The endpoint supports OpenSearch's conditional document writes and external target versions;
see gateway.md for the available API parameters. Existing backfilled target documents were
written once, with internal version 1. The existing metadata record need not contain any
ownership fields.
