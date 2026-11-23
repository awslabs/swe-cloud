# refresher

Keeps each catalog entity's derived data (search document, price rollups) in step with its edits.

When the catalog service changes an entity it bumps `catalog_entities.dirty_seq` for it and then
runs `refresher dispatch` for that entity. A worker later runs the refresh command, which reads the
entity's current state and records the `dirty_seq` it saw as `processed_seq`. An entity is *fresh*
when `processed_seq == dirty_seq`.

Edits come in bursts, and one refresh reads whatever state exists when it starts, so we do not
want a queued refresh per edit. Refreshes of one entity must not run concurrently either.

## Guarantees

1. **One waiting copy per entity.** `dispatch` takes the entity's dedup lock
   (`refresh_unique:RefreshEntity:<entity>` in the unique-lock table) and only queues the job if it
   got it. While a dispatched refresh is waiting in the queue and its lock has not lapsed, further
   dispatches for that entity are dropped (`DROPPED`, exit 0). That is safe because the waiting
   refresh has not started yet and will read the latest state when it does.
2. **The lock is given up when the refresh starts.** A worker releases the dedup lock right before
   it runs the refresh command, so a dispatch made after a refresh has started is accepted (the
   running refresh may have read the entity before that edit).
3. **No overlapping refreshes.** The overlap guard holds a second lock
   (`refresh_overlap:RefreshEntity:<entity>`) while a refresh runs. A worker that receives a job
   while another refresh of the same entity is running puts it back on the queue for
   `RELEASE_AFTER` seconds without running it. That job has not started, so it is still the waiting
   copy and keeps its dedup lock.
4. **Retries.** A refresh command that exits non-zero is retried after `BACKOFF` seconds. A retry,
   like a put-back, is the same SQS message becoming visible again (`ChangeMessageVisibility` on
   the delivery's receipt handle); the worker never re-enqueues. A message delivered `MAX_TRIES`
   times without succeeding is deleted and logged as `job failed`.
5. **Lock lifetime.** The dedup lock lapses after `--unique-for` seconds (default `UNIQUE_FOR`). That
   only bounds how long one stuck copy can block dispatches: once it has lapsed, the next dispatch
   is accepted and takes the lock, and from then on that dispatch's copy is the waiting one.

Every edit is therefore covered by a refresh that starts after it: either the one its dispatch
queued, or the one that was already waiting.

## Running it

Everything talks to AWS through the default boto3 configuration (`AWS_ENDPOINT_URL`,
`AWS_DEFAULT_REGION`, credentials). In this sandbox start the local emulator first:

```bash
start_floci.sh                      # local AWS endpoint on http://localhost:4566
cd /app
python3 -m refresher provision      # queue + tables, idempotent; prints the queue URL
export REFRESH_QUEUE_URL=...        # or leave unset to look up REFRESH_QUEUE_NAME
python3 -m refresher worker --id 1 &
python3 -m refresher dispatch --entity sku-123
```

`dispatch` prints one JSON line: `{"status": "ACCEPTED", "entity": ..., "message_id": ...}` or
`{"status": "DROPPED", "entity": ...}`. Options: `--delay S` (the message becomes visible after S
seconds), `--unique-for S` (dedup lock lifetime). `worker --id N` runs until SIGTERM, finishing the
delivery in hand first.

In production several workers run at once on different hosts, and dispatch runs inside the catalog
service processes. They share nothing but the queue and the tables.

## Configuration

| variable | default | meaning |
|---|---|---|
| `REFRESH_QUEUE_URL` | lookup of `REFRESH_QUEUE_NAME` (`refresh-jobs`) | the job queue |
| `UNIQUE_LOCK_TABLE` | `refresh_unique_locks` | dedup locks |
| `OVERLAP_LOCK_TABLE` | `refresh_overlap_locks` | overlap-guard locks |
| `ENTITY_TABLE` | `catalog_entities` | used by the default refresh command |
| `UNIQUE_FOR` | `600` | default dedup lock lifetime, seconds |
| `RELEASE_AFTER` | `5` | put-back delay when another refresh is running |
| `OVERLAP_EXPIRES` | `120` | overlap lock lifetime, seconds |
| `BACKOFF` | `2` | retry delay after a failed refresh |
| `MAX_TRIES` | `10` | deliveries before a job is failed |
| `RECEIVE_WAIT` | `1` | long-poll seconds per receive |
| `REFRESH_CMD` | `python3 -m refresher.recompute` | the refresh command |

The worker runs `REFRESH_CMD` (split shell-style) with `--entity <id> --message-id <SQS MessageId>
--attempt <delivery count>` appended, in the worker's environment. Exit 0 is success. Ops point
`REFRESH_CMD` at the real recompute tool; `refresher.recompute` is the stand-in used for local runs.

The tables and the queue are provisioned by ops exactly as `refresher provision` creates them
(three tables keyed by a string `pk`, one standard queue); nothing else exists in production.

## Layout

| module | what |
|---|---|
| `dispatch.py` | the `dispatch` command |
| `jobs.py` | the job and its message body |
| `unique.py` | the dedup lock |
| `locks.py` | expiring DynamoDB locks |
| `worker.py` | receive loop, retries, failures |
| `queue.py` | one SQS delivery |
| `handler.py` | runs a delivery through middleware and the refresh command |
| `middleware.py` | the overlap guard |
| `recompute.py` | local stand-in refresh command |
| `provision.py` | queue and table creation |
