# Audit ingress

`publisher.py` receives audit events from request handlers and publishes one
uncompressed, keyed record per call to the two-partition `ledger` topic on MSK.
The cluster in this environment runs Redpanda. `kwire.py` is a synchronous wire
codec; producer sequencing, cancellation and retries belong to this service.
`delivery.py` owns the process session and the partition buffers.

Start a development broker using `python3 /app/provision.py`. It prints its
bootstrap address. Set `KAFKA_BOOTSTRAP` to that address, `TOPIC=ledger`, and
`PORT=8080`, then run `python3 /app/publisher.py`. AWS APIs use
`AWS_ENDPOINT_URL=http://engines:4566`, region `us-east-1`, credentials `test/test`.
The venue has already been started; `floci-venue up` is safe to repeat.

## Service contract

GET `/healthz` returns HTTP 200 once the publisher has a producer identity,
within 30 seconds of startup against a ready cluster.
POST `/publish` takes JSON:

```
{"id":"request-event-id", "partition":0, "value":"audit payload", "deadline_ms":2000}
```

Valid requests get HTTP 200 and either:

- `{"status":"acked","partition":0,"offset":42}`: this exact key and value
  must exist at that partition offset.
- `{"status":"failed","error":"description"}`: the caller did not get an
  acknowledgement. The record may already be stored, or may still arrive from
  a request already sent. It must never be stored twice. Stop sending the
  failed record when answering; the edge allows 100 ms for send/answer overlap.

The caller has a hard deadline. Answer within `deadline_ms`, with 500 ms of
scheduling tolerance. Waiting for certainty beyond this bound is not acceptable.
Unfaulted traffic following a failed call must continue to be acknowledged.
Retry transient broker errors while a call still has time; a transient error
alone is not a reason to fail a live call.
Simultaneous calls to different partitions must make progress independently;
the service must handle at least two concurrent calls. Each partition has
capacity for four admitted calls with outstanding broker replies. An admitted call must not wait for an earlier call's reply before
sending its own request: the gateway can hold replies after forwarding writes.
Once a partition has acknowledged traffic in its current producer namespace,
the first four concurrent calls on an otherwise idle partition are admitted;
calls beyond that capacity may queue, and their deadlines still apply. Calls
that have completed behind an unresolved earlier call can continue occupying
the admission window. Establishing the first sequence of a fresh producer
namespace may serialize admission until that first write is confirmed. Expiring
one call must not fail other admitted calls whose own deadlines still leave
time to complete; their deadlines are independent. Requests carry distinct ids;
application retries of a failed id and process restarts are outside this service's current guarantee.

## Deployment constraints

The bootstrap supplied by operations is the only Kafka endpoint the publisher
may use. During incident testing it names a network gateway rather than the
broker. Produce through that gateway with acks=all, nontransactional idempotent
batches, Produce v3 through v8. Preserve key=id and value=value. Do not substitute
another durable store or bypass the gateway. No filesystem shared with another
role is provided. The gateway can close connections, delay requests and replies,
and return retryable broker errors even when a write has reached the log.

The producer allocation quota for a process is two initial identities plus one
additional identity for each call failed after a send. The number of InitProducerId calls
and the number of distinct (producer id, epoch) pairs on the wire must each
stay within that limit.
Healthy calls do not earn allocations. This quota supports either a process
session or separate partition sessions; avoid identity churn on ordinary traffic.
Transactions are not provisioned. The verifier starts a new process with fresh
cluster state; no process-restart persistence is required.

## Broker operations notes

The broker's per-partition producer state remembers the last five batch sequence
ranges. A repeated range in the same producer epoch gets the earlier offset;
it does not compare record keys or values. A new range must follow the previous
sequence; a gap is rejected with OUT_OF_ORDER_SEQUENCE_NUMBER (45).
That rejection describes this attempt, not the fate of earlier requests.
Clients start a new producer or a higher epoch at sequence zero. An unknown
producer on this Redpanda build can accept a nonzero initial sequence; do not
rely on an out-of-order first request being rejected. Epoch state is per partition, even for a producer that writes multiple partitions.

With acks=all, errors such as timeout, leadership movement, and storage errors
can arrive after the batch was appended. No reply also does not settle whether
it was appended. A request can remain in the network after its caller has gone.
These are ordinary deployment conditions, not evidence that the broker discarded
an event. Client-generated offsets and a successful HTTP reply alone cannot
prove delivery; audits must compare to consumed data.
