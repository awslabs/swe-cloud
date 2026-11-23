# Table lock

Every process that commits to a lakehouse table holds the table lock while it commits. The lock
is one JSON object in the table's bucket, for `orders`:
`s3://lakehouse-orders/orders/_locks/table_lock.json`. The code is `tablelock/`.

## The lock object

```json
{"expired": false, "validUntil": 1761500000000, "owner": "orders-ingest"}
```

| key | meaning |
|---|---|
| `owner` | the writer name the lock was taken under (`--owner`) |
| `expired` | `true` once the holder has released it |
| `validUntil` | epoch milliseconds at which the lease ends |

These three keys are required. Readers ignore any other keys, so a writer may store more.

A version of the lock object is **held** while `expired` is `false` and
`now <= validUntil + 500 ms` (500 ms is the clock-drift buffer). Every process uses this same
test before taking the lock over.

Rules every writer follows:

- Every write is a `PutObject` with `If-Match` on the ETag of the version the writer last read
  or wrote. Storage applies it only if the object is still that version, otherwise it answers
  `412 Precondition Failed`.
- **Acquire**: read the object; if no version is held, overwrite it, `If-Match` on the ETag just
  read, with `expired=false`, the writer's `owner` and `validUntil` = now + lease.
- **Release**: rewrite the held version (`If-Match` its ETag) with `expired=true` and the same
  `validUntil`.
- The object is never deleted and never recreated. `bootstrap.py` creates it once, released.
- The lease is the table's setting (`--lease-seconds`, 20 s for `orders`); commits are far shorter,
  so leases are not renewed.

## Who takes this lock

| process | `--owner` | what it commits |
|---|---|---|
| `orders-ingest` lockworker | `orders-ingest` | ingest batches |
| `orders-ingest` compaction sidecar | `orders-ingest` | compaction commits |
| `orders-backfill` lockworker | `orders-backfill` | backfill batches |

The compaction sidecar is part of the `orders-ingest` deployment: it runs the same `tablelock`
code from the same image on the same host, and it locks under the job's name, like everything
the job runs. It commits to the same ledger as the lockworkers, under worker name `compactor`.

## Dangling-lock alert

A writer that finds a version with `expired=false` whose lease has run out (`now > validUntil +
500 ms`) logs `Found a dangling expired lock with owner ...` and takes the lock over; the alert
pages on that line. A dangling lock means every writer on the table sat idle for up to a lease.
