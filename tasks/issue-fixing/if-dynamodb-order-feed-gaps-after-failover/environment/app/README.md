# Order feed worker

This is the single-table stream worker used by the order integration. It consumes
DynamoDB Streams and writes progress to a separate DynamoDB table. There is no
snapshot stage, multi-table mode, shard rotation or retention-expiry recovery in
this deployment. The source table uses a string `id` hash key and streams
NEW_AND_OLD_IMAGES. Every stream change, including a deletion, is a delivery.

Start the local AWS endpoint with `bash /usr/local/bin/start_floci.sh`. It is Floci
2.2.0 at http://localhost:4566, region us-east-1, credentials test/test. boto3 and
the AWS CLI are installed. The worker does not provision tables. For local operation,
`python3 /app/provision.py` creates empty `orders` and `cdc_checkpoints` tables;
its help lists table-name overrides.
The checkpoint table has string HASH `StreamArn` and string RANGE `ShardID`.

For an existing pair of tables:

```
python3 /app/cdc_reader.py --source-table orders --checkpoint-table cdc_checkpoints --start-from latest --checkpoint-limit 1000
```

`--endpoint-url` and `--region` override the defaults. Table names are deployment
parameters. Checkpoints are scoped by the source's LatestStreamArn, so unrelated
streams can share a checkpoint table. Historical shard rows consist of StreamArn,
ShardID and a string SequenceNumber; those rows must remain readable. Additional
metadata in that partition is allowed. No local journal or other AWS service is
part of the deployment's recovery contract, and the source is read-only to the
worker. Processes must not fork a surviving consumer.

## Subscription behavior

`oldest` reads the retained stream from the beginning on a fresh subscription.
`latest` excludes changes predating the subscription's first start. This exclusion
survives replacements even if the subscription has not yet persisted a shard
checkpoint. Once started, all outstanding changes and changes written during
worker downtime remain eligible for delivery across any number of replacements.
Duplicate delivery of uncheckpointed progress is acceptable; silently skipping
an unacknowledged change is not.

The service's creation timestamps are approximate and have one-second resolution.
A time-based boundary may conservatively include changes in the same second as
first startup. Backlog in earlier seconds must stay excluded. GetRecords can
return an empty page with a next iterator; this does not mean the shard is closed.
LATEST iterators are opaque handles, not durable sequence positions. The pinned
Floci endpoint rejects AT_TIMESTAMP; use the supported stream iterator types.

## Process protocol

The worker emits `READY` once launch positioning is complete. It must be safe to
replace the process after this barrier, even if no delivery has happened yet:
a latest subscription's recovery information must already exist in the checkpoint partition.
The launcher may immediately begin writing source records at READY.

Each change is a flushed stdout line:

```
DELIVER {"id": "order-43", "seq": "12345", "shard": "shardId-..."}
```

Send `ACK 12345` followed by a newline on stdin to acknowledge that delivery.
ACKs can arrive out of order. Repeated or unknown ACKs do nothing. Per shard,
only the highest contiguous acknowledged sequence may be checkpointed. Reading
or delivering a change is not an acknowledgement. Sequence strings are numeric
and may have different lengths; lexical comparison does not order them.

`--checkpoint-limit` bounds normal checkpoint write frequency: wait for at least
that many new ACKs since the previous write, and require the contiguous frontier
to advance. Outstanding later ACKs cannot carry the checkpoint over a gap. Startup
position metadata is separate from these batched progress writes. SIGTERM stops
polling, consumes ACKs already sent on stdin, flushes only acknowledged contiguous
progress (even below the limit), and exits zero. It must terminate without waiting
forever for outstanding deliveries. SIGKILL performs no shutdown work. A replacement
resumes after the durable sequence where one exists. Poll often enough for normal
local deliveries to arrive within 15 seconds, READY within 20 seconds, and graceful
exit within 10 seconds.

## Rolling replacement

A launch acquires the subscription before READY. This supersedes its predecessor;
there is no requirement to wait for that predecessor to exit. Startup can overlap
another startup, an ACK handler, or a SIGTERM flush. The launcher serializes its
routing changes at READY, but cannot cancel a predecessor's already pending AWS
requests. This also happens when a suspended process wakes after a replacement.

The downstream sink stages output within a worker's tenure. A successful shard
checkpoint commits that worker's contiguous acknowledged staging. At takeover,
only progress already durably committed survives; all earlier workers' remaining
ACKs and pending writes cease to authorize progress. After acquiring the
subscription a successor may emit READY only when predecessor writes can no
longer change the recovery position. Replacing again must replay changes that
only a superseded worker acknowledged. A superseded worker may exit zero or
remain idle until signalled; it must never reacquire ownership implicitly from
an ACK or Close. No new delivery is required from a superseded worker.

Worker ownership is transient; subscription positioning lasts across handoffs.
The checkpoint partition is shared by both. Current deployments store an `Owner`
string in the `__worker__` metadata row. The layout may be migrated or replaced;
historical shard rows still have the schema above. A different stream's takeover
must not affect this subscription.

The launcher optionally sets `FEED_CONTROL_SOCKET` to a Unix socket. Keep the
existing `coordination.rendezvous` calls (or equivalent announcements) at their
logical positions, including retries: after reading launch state and before
applying a checkpoint. Announcements contain a stage and the worker's local facts;
the process waits for any reply before continuing. With the variable unset this
instrumentation is a no-op. It is transport for launcher scheduling, not durable
state. An implementation that cancels a write before that point need not announce
that cancelled write. Ordinary operation needs no socket.

If a predecessor commits progress while a successor is still starting, that
commit is part of the durable position the successor acquires. A replacement
resumes strictly after committed shard positions; duplicate delivery is allowed
for uncommitted progress. A launch that loses an ownership race during its own
startup retries from fresh state; every launch must still reach READY unless it is signalled.
