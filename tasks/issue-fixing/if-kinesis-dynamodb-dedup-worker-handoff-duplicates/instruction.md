# Delivery pipeline: messages lost, and messages delivered twice, around partition replacements

You are on call for the delivery pipeline. Two tickets are open and both point
at the same part of the system.

## The system

`/app/worker.py` is a partition worker. Messages arrive on a per-partition
input stream, each tagged with a `messageId` chosen by the client, and routing
sends a given `messageId` always to the same partition. For its partition the
worker keeps the set of keys it has already accepted: a message whose key the
partition has already accepted is not sent on, and its input position is
accounted for; a message with a new key is sent to the delivery stream through
the local append service, and its position is accounted for once that is done.

Delivery is counted on the delivery stream itself. A message counts as
delivered when its record is in that stream, and as delivered twice when two
records with the same `messageId` are in it. The append service writes one
record per request and answers with the sequence number the stream assigned.

The partition supervisor runs one worker per partition. When a worker stops
responding, the supervisor kills it without warning and starts a replacement on
the same partition: same input stream, same delivery stream, same rows in the
state table, same partition id. The replacement is told nothing beyond its
assignment. `/app/CONTRACT.md` describes the protocol, the state table and the
append service API all of this depends on.

## What we have been seeing

Both tickets are for messages that were in flight at the moment a partition
changed hands, and both date from the failover drills.

**Tuesday.** After a drill replaced the worker for a partition, the partition's
checkpoint reported the partition fully processed, but a message accepted just
before the replacement never turned up in the delivery stream. Not late —
absent. The count for that partition came up one short, and the state table
still had a row for the missing message.

**Thursday.** During a replay of a partition's input after a replacement,
downstream opened a duplicate-delivery ticket: one `messageId`, two records in
the delivery stream, different sequence numbers. The customer was billed twice.

Messages that arrive after a replacement has settled are handled correctly,
and the drills are clean whenever no replacement happens. Neither ticket
reproduces on a partition whose worker was never replaced.

## What has to hold

1. Every message we accept reaches the delivery stream exactly once, with the
   payload it arrived with. The count is taken on the delivery stream, not on
   the worker's own bookkeeping.
2. After a replacement the partition must end up fully processed: its
   checkpoint at the last input position we submitted, no key left in an
   undecided state, and the worker able to shut down cleanly when asked.
3. Recovery is bounded. Within 30 seconds of a replacement starting, a message
   that was in flight has to be in the delivery stream. Within 45 seconds the
   partition must be fully processed.
4. Nothing already in the delivery stream before a replacement may be put there
   again by it.

## Constraints

The pipeline around the worker is not ours to change.

- The control protocol, the state contract and the append service API in
  `/app/CONTRACT.md` stand as written. The worker has to keep to them, including
  announcing the pause after the partition's row for a key is durable and
  before the publication request for that key, and answering `QUIESCE`.
- Everything the worker publishes goes through `POST /publish`.
- The worker runs as one process in its own working directory, and nothing it
  starts may outlive it. Anything it needs after a replacement has to live in
  the state table or in the streams.
- Workers are killed without warning and get no chance to clean up. Assume
  nothing about how far a killed worker got with what it was doing.

## Reproducing and inspecting

```
cd /app
python3 run_pipeline.py                 # one clean pass: provision, ingest, run, print
python3 run_pipeline.py --ids 5 --copies 3
```

That pass replaces nothing and judges nothing; it is a way to watch the pieces
run against the local endpoint. Everything it prints comes from the streams and
the state table, so the same inspection works on any run you set up yourself,
and the input and delivery streams of any run are readable the same way.
