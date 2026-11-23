# orders table writers

- `lockworker` — runs one writer's commits against a table, one lock acquisition per commit.
- `tablelock/` — the table lock (see `docs/LOCKING.md`): `storage.py` talks to S3,
  `provider.py` acquires and releases, `ledger.py` is the commit made under the lock.
- `bootstrap.py` — creates the bucket and seeds the lock object and the ledger counter.

Storage is S3 at `$AWS_ENDPOINT_URL` (`http://localhost:4566` here), region `us-east-1`,
credentials `test`/`test`; boto3 reads all of it from the environment.

## The storage gateway

In production `AWS_ENDPOINT_URL` points at the S3 gateway in front of the table's bucket, and
every read and write a writer makes (lock object, ledger, results) goes through it. Under load it:

- answers `503 Service Unavailable` without forwarding the request;
- forwards the request and loses the backend's answer, so the client gets a `503` or a dropped
  connection for a request storage did apply;
- answers `504 Gateway Timeout` when the backend is slow to answer. The gateway does not cancel
  that backend request: it is still applied when the backend gets to it, which under load can be
  seconds after the client saw the `504`.

Locally there is no gateway: `localhost:4566` is the storage backend itself.

## Running

```bash
python3 /app/bootstrap.py
COMMON="--bucket lakehouse-orders --lock-key orders/_locks/table_lock.json \
  --ledger-prefix orders/ledger/ --results-prefix orders/results/ \
  --sections 4 --lease-seconds 20 --hold-ms 300 --poll-ms 100 --acquire-timeout-s 15"
/app/lockworker --worker-id ingest   --owner orders-ingest   $COMMON &
/app/lockworker --worker-id backfill --owner orders-backfill $COMMON &
wait
```

Each lockworker is its own process. In production each runs in its own container with its own
empty working directory, so nothing on local disk is shared between them, or kept between runs.

## What a run leaves in the bucket

- `orders/ledger/counter.json` — `{"seq": n}`, the last sequence number committed.
- `orders/ledger/entries/<seq:06d>-<worker>-<section>.json` —
  `{"seq", "worker", "section", "lock_etag"}`, one per commit; `lock_etag` is the lock version the
  commit was made under.
- `orders/results/<worker-id>-<section>.json` — `{"acquired": bool, "released": bool,
  "lock_etag": str | null}`, written after each section; `released` is whether the lock was
  released at the end of that section.
- the lock object itself.

## Settings for `orders`

| flag | value |
|---|---|
| `--lease-seconds` | 20 |
| `--hold-ms` | 300 |
| `--poll-ms` | 100 (re-read a held lock at least this often) |
| `--acquire-timeout-s` | 15 |
