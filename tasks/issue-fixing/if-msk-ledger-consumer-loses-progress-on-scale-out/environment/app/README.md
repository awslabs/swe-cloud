# Ledger delivery service

`worker.py` overlaps accounting requests through a bounded dispatcher and manually
commits delivery progress. The service has no idempotency key: every successful POST is a real
application, even for an offset previously seen by a different worker. Graceful
changes in capacity must neither replay a ledger entry nor leave progress stranded
on a now-idle partition. Crash recovery and process kills are outside this incident.

Start a development broker/topic with `python3 /app/provision.py`. Produce ordinary
records with `confluent_kafka.Producer` (values are opaque to this worker). Run:

```
python3 /app/worker.py --worker-id W1 --bootstrap HOST:PORT \
  --group GROUP --topic ledger --effects-url http://HOST:PORT
```

The accounting endpoint accepts `POST /apply` with JSON fields `worker_id`,
`member_id` (the consumer's memberid()), `partition`, `offset`; HTTP 200 means the
entry was applied. An endpoint is provided by the deployment, not by the worker.
The worker prints `READY <worker-id> <member-id>` after its first assignment.

## Operating contract

The fleet uses classic cooperative-sticky groups, dynamic membership, and the
configuration in `consumer_config`. Keep that protocol, manual commits, and the
worker CLI. Do not introduce auto-commit, transactions, static membership or a
deduplicating sink. Bootstrap may be a routing gateway; all worker connections
must follow the advertised gateway endpoints. Each process starts once and stays
in the group through a graceful scale event. Keep the same live consumer/member;
a successful scale-out should settle in the protocol's normal two rounds, without
another join caused by application error handling.

Accounting calls can take up to 1.5 seconds and complete in a different order
from Kafka delivery, including within one partition. HTTP 200 acknowledges only
that particular call. The dispatcher was introduced to meet the existing service
budget: a burst of up to 18 records on an already owned partition must be applied
within five seconds of publication, even when some calls take the full latency.
There may be six busy partitions. Keep delivery bounded (the shipped window is
eight outstanding records per partition); serializing all accounting calls does
not meet this deployment's capacity needs. Record values remain opaque.

Commit only a completed delivery prefix: a higher offset's successful HTTP call
cannot acknowledge an earlier call that is still outstanding. Use synchronous
explicit manual commits. Once a completed prefix advances, attempt its next offset
within one second; one attempt may cover several completed records. A rejected
attempt doesn't undo any accounting operation. Once the group settles and HTTP
calls finish, committed offsets must catch up within five seconds even if there
is no more data. Dispatching an HTTP call is not evidence it has been applied.
A worker must account for ownership changes before another member resumes from
OffsetFetch. Retained partitions continue serving during cooperative transitions:
do not suspend the entire worker waiting for an assignment notification. In our
small local deployment a record on an owned partition should be applied within
five seconds. Normal control-plane requests can take several seconds during a
scale event. Keep polling so callbacks can run. The pinned client is already a
supported librdkafka release; replacing the client library is not the repair.

Graceful revocation and SIGTERM must drain accounting calls already dispatched,
as well as completed progress, before releasing ownership. Close the consumer and
exit cleanly on SIGTERM.
Workers run in separate working directories; local files cannot coordinate them.
The effects service, Kafka gateway and broker belong to operations. Repair the
worker; do not replace those services or intercept their observations.
