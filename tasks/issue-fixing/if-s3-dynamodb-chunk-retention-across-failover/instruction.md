# Retention sweep stalls after compactor failover

Team, we need the chunk store's retention service fixed. Storage audit flagged
it after last week's failover drill; the drill report is below.

## What the drill showed

The drill exercised a routine retention run with three forced handovers of
the service (the orchestrator killed the running placement hard and started
a replacement, three times, as it does during real node moves). The run's
deletion batch never completed:

- the batch record is still sitting short of `DONE`;
- two chunk objects that the live index no longer references are still in the
  bucket, and nothing has come back to remove them;
- the index itself is fine — the published snapshot is correct and queries
  against it work.

Before anyone asks: yes, the objects are old enough, and the grace the run
was configured with is in the run request. The service is supposed to finish
this work on its own.

## Background you should have

An earlier build of this service failed a drill in the opposite direction
about six weeks ago: queries started returning not-found for entries the
serving tier was still resolving. That is why the deletion grace exists. The
query frontends keep serving a previous index snapshot until **6 seconds
after** a new snapshot becomes authoritative, and a query already running at
that point may take up to **1 second more** to finish its object reads (the
service README documents this). Whatever you change must not bring that
failure mode back — during the drill, and in general.

Facts from the drill timeline, for calibration:

- the prepared batch was held **12 seconds** before its publication went
  live;
- the first placement was killed about **2 seconds after** publication
  committed, before it recorded anything further;
- each replacement placement started within about **1 second** of the
  previous death, with the next epoch, per policy with a fresh empty work
  directory — the killed placement's disk is destroyed, always;
- the second replacement was killed about **29 seconds after**
  publication while it was still holding the one remaining chunk, and
  its replacement started within about **1 second** like the others;
- the drill's completion budget was **36 seconds after** publication.

## What must hold afterwards

1. **The work finishes.** Whatever a placement has published must be finished
   by its replacements: every chunk named by the batch's deletion marker is
   physically gone from the bucket and the batch record reads `DONE`, within
   the run's completion budget — no matter how many placements die on the
   way, including one that dies immediately after a successful object delete
   and before it can record anything.
2. **Deletion does not outrun the serving tier.** While any snapshot —
   authoritative or still cached — references a chunk, or a bounded query can
   still be reading it, that chunk must remain fetchable with its original
   bytes. The configured grace (8s for these runs, from the run request) is
   the store's safety margin and must be honored; do not shorten it.
3. **What is not authorized survives.** Retained chunks keep their bytes,
   staged uploads that no index references yet are left alone, existing index
   snapshots are immutable, and the deletion marker for a batch stays
   readable under `markers/` after completion — the weekly audit reconciles
   swept objects against it.
4. **Retries are fine.** There is no exactly-once contract on delete calls;
   an object that is already gone counts as deleted.

## Fixed constraints

One active placement at a time; placements are replaced without warning; each
replacement gets a fresh empty work directory and the predecessor's disk is
destroyed (this policy is not changing). Anything that must survive a
placement belongs in the bucket and table the run was given. Keep the pause
points the service already announces — startup, after preparing a batch, after
publication commits, after each successful object delete — announcing them with
the same details and still waiting at them; the deploy harness depends on them.

## How to run it

Everything is in `/app` (see the README there). For a local rehearsal against
Floci on `http://localhost:4566`:

```bash
cd /app
python3 seed.py --out /tmp/manifest.json
python3 worker.py --endpoint http://localhost:4566 --region us-east-1 \
    --bucket <bucket> --table <table> --run-id <run> \
    --worker-id w1 --epoch 1 --work-dir /tmp/w1
```

An uninterrupted run should publish the new snapshot, remove the expired
objects once it is safe to do so, and leave the batch `DONE` with the marker
still readable. The graded environment runs the same program under the
placement schedule described above.

Fix the service. The code in `/app` is yours to change.
