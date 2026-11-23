# Ticket: dangling table locks on `orders` since the storage gateway got flaky

## What's happening

Writers to the `orders` lakehouse table serialise their commits with a single S3 lock object,
`s3://lakehouse-orders/orders/_locks/table_lock.json`, guarded by `If-Match` on its ETag. The code
is in `/app` (`tablelock/`, driven by `/app/lockworker`); `/app/docs/LOCKING.md` is the contract.

Over the last few nights the S3 gateway in front of this table has been shedding load. Since then
the dangling-lock alert (`Found a dangling expired lock with owner ...`) has been paging
repeatedly, and every page stalls all writers on the table for a full lease. We pulled three
incidents into `/app/ops/2026-10-24-orders-lock.log`. In each one a writer's lock write came back
as a `503` or a dropped connection ("Remote end is closed") — **and in two of them the lock object
afterwards showed that the write had actually taken effect.** Yet the writer treated it as a
failure: one sat on its own fresh lease until it lapsed, one gave up releasing after a single try,
and one reported `FAILED_TO_RELEASE` and raised a steal alert for a release that had in fact landed.

The gateway's errors are not clean rejections. A `5xx` or a dropped connection on a write says
nothing about whether, or when, the write reaches storage.

## What we need

Make the `orders` writers ride out these ambiguous gateway answers without stalling the table and
without ever stepping on another writer's live lease. After your change, with the gateway
intermittently losing answers to writes:

- No writer stalls behind, or raises a steal alert for, a lock write of its own that actually
  landed. A commit that acquired must proceed; a lock that was released must be reported released;
  recovery must happen quickly, not after the lease elapses.
- Mutual exclusion still holds at every instant: a commit runs only while this writer holds a live
  lease, and no writer ever adopts, renews or expires a lock version that another writer wrote and
  still legitimately holds.
- Every commit still lands exactly once in the ledger (`orders/ledger/`): no sequence number is
  lost or used twice.

## Keep working

- `/app/lockworker` keeps its command-line interface, writes the same ledger records and the same
  `orders/results/<worker>-<section>.json` reports, and exits 0 after its sections.
- The lock contract in `/app/docs/LOCKING.md` is fixed: the three required body keys, every lock
  write conditional on `If-Match`, release = write `expired=true` keeping the same `validUntil`,
  the object is never deleted or recreated, and the lease length. The commit keeps its records and
  its work (`--hold-ms`). Do not weaken the lease or shorten the commit to dodge the problem.
- Each worker runs as its own process with its own empty working directory; nothing is shared on
  local disk between workers or between runs. All storage goes through the S3 endpoint in the
  environment (`AWS_ENDPOINT_URL`), as the current code already does.

## Running it

```bash
python3 /app/bootstrap.py
COMMON="--bucket lakehouse-orders --lock-key orders/_locks/table_lock.json \
  --ledger-prefix orders/ledger/ --results-prefix orders/results/ \
  --sections 4 --lease-seconds 20 --hold-ms 300 --poll-ms 100 --acquire-timeout-s 15"
/app/lockworker --worker-id ingest   --owner orders-ingest   $COMMON &
/app/lockworker --worker-id backfill --owner orders-backfill $COMMON &
wait
```

`python3 /app/lockworker --help` documents every flag. The committed changes under `/app` are what
we grade.
