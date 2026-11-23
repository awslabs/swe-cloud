# fenced_worker

Single-writer appender for the reconciliation ledger. Several `fenced_worker` processes run on
different hosts; exactly one of them holds the ledger lock at a time and appends one entry to the
ledger per work interval. The others wait as standbys and take over when the holder goes away.

```
python -m fenced_worker --worker-id worker-1 --endpoint http://127.0.0.1:4601 \
    --table locks --lock-key ledger-lock --bucket fenced-ledger --ledger-key ledger.json \
    --lease-ms 6000 --heartbeat-ms 1000 --refresh-ms 500 --buffer-ms 1000 \
    --work-interval-ms 500 --journal ./journal.jsonl
```

The process prints `READY` on stdout once its AWS clients are built, then loops forever: acquire
the lock (or wait as a standby), append while holding, go back to waiting if it stops holding. It
is stopped with SIGTERM.

On SIGTERM the worker releases the lock before it exits (`LockClient.close()`, as the Go client's
`Close()` does), so that a standby can take over at once instead of waiting out the lease. Deploys
restart the workers one at a time this way, holder included, and a deploy must not stall the
ledger: within 5 s of the holder's SIGTERM a standby holds the lock and has appended.

## Deployment facts

- Every worker reaches AWS only through its host's egress proxy, which is what `--endpoint` points
  at (also exported as `AWS_ENDPOINT_URL`). Region `us-east-1`, path-style S3.
- Each worker is its own process with its own working directory, on its own host. There is no
  shared local disk: the lock item and the ledger object are the only shared state.
- The egress policy for the lock table allows `GetItem`/`PutItem`/`UpdateItem`/`DeleteItem`.
  `TransactWriteItems` and `BatchWriteItem` touching the lock table are refused.
- Owner names are `<worker-id>:<nonce>` with a fresh nonce per process start, so two processes
  never share an owner name, even across restarts of the same worker id.

## The lock (`fenced_worker/lockclient.py`)

A port of the Go lease-lock client our other services use, with the same item layout so both can
share a lock table. One item per lock in the lock table (hash key `key`, string):

| attribute | meaning |
|---|---|
| `ownerName` | current holder, `<worker-id>:<nonce>` |
| `recordVersionNumber` | opaque version tag; every heartbeat writes a fresh one |
| `leaseDuration` | lease in milliseconds (number) |
| `fence` | epoch number: 1 for the first holder, +1 on every acquisition (number) |

- **Heartbeat.** The holder renews every `--heartbeat-ms` with an `UpdateItem` that sets a new
  `recordVersionNumber`, conditioned on the item still carrying the holder's owner name and the
  version tag it last wrote. A heartbeat never changes `ownerName` or `fence`.
- **Takeover.** A standby reads the item with a consistent read every `--refresh-ms`. When the
  `recordVersionNumber` it saw has stayed unchanged for `leaseDuration + --buffer-ms`, counted on
  the standby's own clock from the moment its read returned, it replaces the item with a
  `PutItem` conditioned on that same `recordVersionNumber`, writing its own owner name, a fresh
  version tag and `fence + 1`. A heartbeat that lands first makes the takeover fail. There are no
  absolute timestamps in the item.
- Failover expectation: a standby takes over no earlier than one lease after the holder's last
  heartbeat that DynamoDB applied, and no later than 9.5 s after it at the default settings
  (lease + buffer + one refresh + 2 s). A slower failover pages on-call; an earlier one is a bug.
- The lease (6 s, six heartbeat periods) is sized so that a holder rides out a DynamoDB brownout
  of a few seconds. During a brownout requests can hang until their timeout, or come back as
  500s or reset connections.

## The ledger (`fenced_worker/ledger.py`)

`s3://<bucket>/<ledger-key>` in a versioned bucket, one JSON document:

```json
{"fence": 2, "entries": [{"writer": "worker-1", "fence": 1, "n": 1}, {"writer": "worker-2", "fence": 2, "n": 1}]}
```

The holder appends `{"writer": <worker-id>, "fence": <its lock fence>, "n": <per-process counter>}`.
After every append that S3 accepted, the worker writes the same object plus `"etag"` as one JSON
line to `--journal` (its local record of acknowledged entries).

The reconciliation job replays `entries` in order and relies on:

1. **Acknowledged entries are permanent.** An entry that any worker journaled is in every later
   version of the ledger.
2. **Epoch order.** Each entry carries the fence of the lock epoch that wrote it, and only that
   epoch's holder writes it. Once an entry with fence N is in the ledger, no entry with a lower
   fence comes after it. The top-level `fence` is the highest epoch that has written.

The job audits every version of the object, not only the latest one.

## Running locally

Floci (the local AWS emulator) answers on `http://localhost:4566`; `start_floci.sh` boots it.

```
aws --endpoint-url http://localhost:4566 dynamodb create-table --table-name locks \
    --attribute-definitions AttributeName=key,AttributeType=S --key-schema AttributeName=key,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST --stream-specification StreamEnabled=true,StreamViewType=NEW_AND_OLD_IMAGES
aws --endpoint-url http://localhost:4566 s3 mb s3://fenced-ledger
aws --endpoint-url http://localhost:4566 s3api put-bucket-versioning --bucket fenced-ledger \
    --versioning-configuration Status=Enabled
echo '{"fence":0,"entries":[]}' | aws --endpoint-url http://localhost:4566 s3 cp - s3://fenced-ledger/ledger.json
```

then start two workers with `--endpoint http://localhost:4566` from two directories.

Emulator quirk: after Floci rejects a request, the next request on the same keep-alive connection
can get a spurious 400/405. Rebuild the client (new connection pool) after any S3 error, as
`Ledger.reset()` does; the real S3 does not do this.
