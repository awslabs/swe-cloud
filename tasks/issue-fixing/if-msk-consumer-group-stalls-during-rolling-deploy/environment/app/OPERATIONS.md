# Orders ingestion operations

Workers consume a single topic, enrich each record, publish the response to a Kafka
receipts topic and checkpoint next offsets to the Kafka group. This is an at-least-once
pipeline. A checkpoint is a durability promise: every earlier input must already have
a successful corresponding publication. Retrying an input after a crash is allowed. MSK is provisioned through the local AWS endpoint;
its data plane is a real Redpanda broker. No other durable service is configured.

Run `python /app/provision.py` to create a development cluster and an orders topic.
It prints a JSON object containing bootstrap, topic and group. Example invocation:

    python /app/worker.py --bootstrap BROKERS --group GROUP --instance-id pod-a --topic TOPIC --output-topic RECEIPTS

Additional workers use different static instance IDs. A replacement uses its predecessor's
ID. `wire.py` contains the classic protocol codec; `group.py` is the membership and
assignor implementation; `worker.py` runs fetch, commit, callbacks and shutdown. `pipeline.py` runs concurrent enrichment and
publication. The provisioner also creates the receipts topic.
The pinned fetch client uses manual assignment; the Python group controller owns the
classic JoinGroup/SyncGroup protocol. Engine APIs are at `$AWS_ENDPOINT_URL`.

## Deployment service objectives

Keep `consumer/cooperative-sticky` as the selected group protocol, static IDs as passed
on the command line, a 45,000 ms session timeout, 30,000 ms rebalance timeout and
heartbeats at least once per second. Consumption commits at least once per second
while there is work. Do not shorten the session to make deployment recovery happen.

A quiet restart, graceful or abrupt, can take up to 38 seconds, still inside the configured session. Throughout that outage and
for 10 seconds after the replacement starts, the group must remain Stable, keep the
static identities present, and keep each instance's coordinator assignment unchanged.
The unavailable worker's cached partitions are intentionally reserved during this time.

Scaling and replacement may overlap. A partition must stop being processed by its old
owner before it can be assigned to another. Cooperative revocation intentionally allows
a short interval without an owner: allow at least 10 seconds for a normal transfer before
initiating any recovery based on incomplete coverage. Do not disrupt an ordinary transfer
while it is inside that interval. Once a replacement starts, full exclusive ownership of
every subscribed partition must return within 30 seconds, and a newly appended batch
must be committed within a further 20 seconds. This applies when a worker is terminated
or killed during a rebalance, not only between rebalances. TERM must exit within 15 seconds
once any in-progress callback is released. The session remains valid throughout recovery.

The optional deployment observer may take up to 20 seconds to release an in-progress
revocation callback, including while TERM arrives. Preserve the starter's notification
payload and ordering when changing the worker: processing and checkpointing precede
the notification, and the group controller resumes after the observer returns. Normal
runs omit the observer.

Coordinator state is available through the standard Kafka admin protocol, including
member subscription metadata, static IDs and stored assignments. Broker responses are
authoritative for group ownership; a computed plan alone is not a broker acknowledgement.

## Processing and receipts

Supply `--output-topic` for durable receipts. The optional `--processor-url` identifies
an HTTP enrichment service. Without it the development transform returns a SHA-256.
With it, POST each input to `/enrich` as JSON with `topic`, `partition`, `offset`,
`value` (base64 input bytes), and `instance_id`. Publish a JSON receipt containing those
fields and `result`, the service's JSON response. Use the actual response; it may include
opaque service-issued fields. Every input needs a receipt, including inputs in flight
when ownership changes. All workers in a group use the same output topic and service.

Production enrichment calls complete independently, sometimes out of input order, and
may take up to five seconds. The starter permits eight simultaneous requests and bounds
its fetched work queue. A routine newly appended batch of up to twelve records per
partition must meet the twenty-second progress objective, including under that latency.
The consumer must keep heartbeating while requests, publication or the deployment
observer are pending. Receipts and checkpoints must make progress while assignments are
otherwise unchanged. Do not treat a fetched offset or an HTTP reply as a durable receipt.

Revocation notifications mark a completed handoff: finish enrichment, durably publish
and checkpoint all fetched work for the revoked partitions before notifying the observer.
No request for a revoked partition may remain in flight at notification time. A new owner
must never process that partition before the old owner has finished its work. Retained
partitions need not be drained to transfer an unrelated partition. A graceful shutdown
finishes accepted work and checkpoints it; abrupt loss can leave unfinished work for the
replacement to retry. The service and output broker remain available during deployments.
