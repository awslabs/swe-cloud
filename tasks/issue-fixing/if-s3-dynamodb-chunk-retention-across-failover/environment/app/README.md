# Chunk store retention service

The chunk store keeps log chunk payloads in one S3 bucket and its index
snapshots beside them; a DynamoDB table holds the small amount of shared
state per run. Retention runs as a two-phase job:

1. **Mark and publish.** The placement loads the run request (`META`),
   rewrites the table's index snapshot without the retention-expired chunk
   references, records the pending deletion set for the batch as a
   deletion marker, and publishes the new snapshot as the authoritative
   one by flipping `HEAD` in a single conditional transaction.
2. **Sweep.** After the run's deletion grace has passed, the chunk objects
   named by the marker are deleted. Deleting an object that is already
   gone counts as success; delete calls may be retried freely — there is
   no exactly-once contract on them.

## Storage layout

```
s3://<bucket>/chunks/<opaque-id>                     chunk payloads
s3://<bucket>/index/<tenant>/<table-id>/<version>.json   immutable snapshots
s3://<bucket>/markers/<batch>.json                    deletion markers
```

DynamoDB (key `pk`/`sk`; a run's items share `pk = RUN#<run-id>`):

| `sk`            | contents                                                  |
|-----------------|-----------------------------------------------------------|
| `META`          | the run request: table, batch, cutoff, grace, prefixes …  |
| `HEAD`          | authoritative index pointer `{version, index_key}`        |
| `BATCH#<batch>` | batch state written by the placement                      |
| `OWNER`         | the placement epoch                                       |
| `AUDIT#…`       | query-path observations written by the frontends          |

## Query path (read this before touching deletion)

Query frontends do not read `HEAD` per lookup. A frontend resolves `HEAD`
once, fetches that snapshot, and serves lookups from it; it may keep
serving the **previous** snapshot until **6 seconds after** a new snapshot
becomes authoritative, and a query already running at that point may take
up to **1 further second** to finish its object reads. Frontends verify
chunk digests, so truncated or rewritten objects fail queries just like
missing ones. A chunk is only safe to delete once no snapshot —
authoritative or still cached — references it, and no in-flight query can
still be reading it.

## Deletion grace

Each run request carries `delete_delay_s` (8 seconds for current runs).
Physical deletion of an authorized chunk must not begin before the
configured grace has been honored. The grace is the store's safety margin
for the query path above; it is set per run by storage ops and is not to
be shortened by the service.

## Deletion markers

The deletion marker for a batch lives at `markers/<batch>.json` in the
chunk bucket and enumerates exactly the chunk keys the batch is
authorized to delete, plus the input and output index versions. It is the
audit record of a retention decision: it must be in place before the
batch's new snapshot is published, and it stays readable under `markers/`
after the batch completes — the weekly storage audit reconciles swept
objects against it, so the service must not remove or rewrite it.

## Placement policy

Exactly one placement of the service is active at a time. The orchestrator
replaces placements freely and without warning: a replacement is a new
process with a **fresh, empty work directory**, and the predecessor's
disk is destroyed when it is replaced — nothing on it survives, by
policy. State that must survive a replacement belongs in the bucket and
table the run was given. Each replacement is started with the next
`OWNER` epoch and receives the same run request.

## Running a rehearsal

With Floci on `http://localhost:4566`:

```bash
cd /app
python3 seed.py --out /tmp/manifest.json          # prints the manifest
python3 worker.py --endpoint http://localhost:4566 --region us-east-1 \
    --bucket <bucket> --table <table> --run-id <run> \
    --worker-id w1 --epoch 1 --work-dir /tmp/w1
```

The placement announces its startup, the point after it has prepared a
batch (new snapshot written, marker recorded, publication not yet
attempted), the point after publication has committed, and the point
after each successful object delete. The deploy harness relies on these
pauses to stage upgrades; any rewrite of the service must keep announcing
and waiting at them.
