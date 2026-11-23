# Event sink operations

This Python service accepts event batches and writes to a managed OpenSearch collection.
The local development deployment uses a real OpenSearch 2.11.1 domain and S3 on Floci.
Run `floci-venue up`, then `python bootstrap.py` to provision an ordinary development
instance. Export the assignments it prints and run `python -m sink` from /app. Bootstrap
uses unique domain and bucket names; creating a domain may take a minute. It prints the
endpoint only after it is reachable. AWS clients use AWS_ENDPOINT_URL and the local test
credentials already in the environment. Use the returned domain endpoint as-is.

GET http://127.0.0.1:8080/healthz is the intake readiness barrier. POST /events accepts a
JSON array and returns 202 after durable buffering in the S3 inbox. Each object has `key`
(business identity), `ts` (ISO date), and `payload` (string). A redelivery can carry
`potential_duplicate: true`, requesting an existence check before another write. There
are no arbitrary extra fields in the strict collection mapping. Invalid dates must reach
the store and be dead-lettered, not be silently discarded by intake validation.

Deployment environment:

| Variable | Meaning |
| --- | --- |
| OS_ENDPOINT | The sole OpenSearch transport endpoint for every read and write |
| WRITE_ALIAS | Configured collection target; normally events |
| QUERY_TERM | Exact-match identity field; normally key |
| DLQ_BUCKET, DLQ_PREFIX | S3 dead-letter location |
| MAX_LATE_APPLY_S | Transport's maximum apply delay after a client-visible ambiguous failure (seconds); currently 8 |
| INBOX_BUCKET, INBOX_PREFIX | Durable intake records; bucket defaults to DLQ_BUCKET, prefix defaults to inbox/ |
| WORKER_ID | Stable supervisor slot identity; defaults to standalone |
| INTAKE_PORT | Local HTTP intake port; defaults to 8080 |
| AWS_ENDPOINT_URL | AWS control plane and S3 endpoint |

Every accepted, unflagged event must have its first bulk dispatched within 1 second.
Bulks may carry at most ten index operations. Transient lookup work must not stop new
intake/bulk delivery. End-to-end delivery is at most 25 seconds from acceptance to search
visibility, or to a permanent-rejection DLQ record. A redelivery must not create a second
copy, including while the first delivery is queued,
in flight, reconciling, or not yet searchable. A business key identifies an immutable event;
redelivery changes only the optional potential_duplicate flag. A flagged event may be a
new key, or may already exist from before this inbox was created. Only the first receipt
of a distinct unflagged key needs the first-dispatch guarantee. SIGTERM stops intake and
exits within five seconds.

The transport sometimes closes a connection without a response, returns HTTP 500/504,
or returns bulk item status 500/504 after a possibly successful write. A failed request
may still be applied for up to MAX_LATE_APPLY_S seconds after the failure is observed by
the client. When the sending process is killed, the same maximum applies from process
exit to any remaining application of its outstanding requests. Other retryable failures
use the normal retry path. An item 4xx except 429 is
permanent: put an object at s3://DLQ_BUCKET/DLQ_PREFIX<key>.json containing the original
event under `event` and the store's item error under `error`. Ambiguous deliveries are
not permanent rejections. Only permanent rejections belong in the DLQ.

The store is near-real-time: acknowledged writes become searchable on an index refresh,
not at write acknowledgement. Refresh runs at each backing index's configured
`index.refresh_interval`; bootstrap sets it explicitly to five seconds. Settings and
alias metadata are readable through OS_ENDPOINT. Search through a collection alias
covers all of its backing indices. Rollover creates a new write index and leaves older
documents in the previous index. Search hits report concrete index names; multi-search
returns one response per search, in request order. The collection may roll over while
this process is running.

Production restrictions also apply locally: use only OS_ENDPOINT for OpenSearch I/O;
use index actions with auto-generated IDs, never custom `_id` or create/update/delete.
Do not force refresh/flush (including refresh query parameters), change settings, remove
documents, or use delete/update-by-query. Those administrative operations belong to the
platform. The sink may search, multi-search, and read settings/alias metadata. It must
prevent duplicate delivery, rather than cleaning up extra documents afterwards.


Process replacement
-------------------
The operator may kill and replace the process without draining it. A 202 response means
ownership has transferred to this deployment: every accepted event must survive process
loss and resolve to one stored copy or its permanent-rejection DLQ object. The next process
uses the same configured inbox and store, with a fresh working directory. The deployment runs multiple live intake workers against the same inbox and collection.
Each worker has a distinct WORKER_ID and INTAKE_PORT. The supervisor never overlaps two
processes with the same WORKER_ID; it reuses that identity when replacing a failed worker.
A peer stays live during replacement. A replacement can itself fail during recovery.

Only S3 and OpenSearch survive replacement. Local files, /tmp and process memory are not
recovery storage. The configured inbox prefix belongs to the sink; its record schema may
change with the repair. The supplied inbox adapter reads and writes JSON records there.
The DLQ prefix is exclusively for permanent rejection objects. Preserve unrelated objects
and prefixes in both buckets. Do not create other AWS resources for delivery state, write
coordination documents to OpenSearch, or inspect any endpoint besides OS_ENDPOINT for
OpenSearch data. Completed identities may remain in the inbox; no garbage collection is needed. Never accept intake before its recovery record is durable.

Unresolved work inherited by a replacement must become searchable or dead-lettered within
25 seconds of that process first reporting healthy, provided it runs uninterrupted for
that interval. The usual acceptance-based 25-second deadline applies to new intake in an
uninterrupted process. Recovery must not stop fresh intake or its first-dispatch guarantee.
Health readiness means intake is available; it must not wait for inherited work to finish reconciliation.
Requests from an exited process can still apply while the replacement is running, including
across rollover. A missing search result alone does not mean such a request was rejected.

Concurrent intake
-----------------
The load balancer may send an event or its redelivery to any worker, concurrently and
without sticky routing. All workers share INBOX_BUCKET/INBOX_PREFIX. A 202 transfers
ownership to the deployment, regardless of which worker answered. Uniqueness and the
delivery deadlines apply across the whole deployment. Worker-local caches are ordinary
process memory; a running peer need not have seen another worker's accepted event.
S3 provides its ordinary strongly consistent object reads and conditional request semantics.
The inbox namespace remains available for any delivery-state schema the repair needs.

The supervisor promptly restarts failed slots with the same WORKER_ID. Work interrupted
by a worker failure has the recovery allowance from its replacement's readiness. Peers
must continue delivering fresh, distinct keys during that recovery, and redeliveries
through a peer must not create extra copies. Worker identities and the shared inbox are
stable for this deployment; slot removal and reassignment are outside its lifecycle.
Ordinary development may run just one worker. To run two, start each with a different
WORKER_ID and INTAKE_PORT while keeping the other exported assignments identical.

Inbox layout and local S3 transport
----------------------------------
Bootstrap provisions 32 JSON catalog pages at INBOX_PREFIX/catalog/00.json through
31.json, initially {"entries":{}}. The supplied adapter hashes a key to a page and
stores its complete delivery record in that page's entries map. These pages are ordinary
sink-owned state; a repair may change their contents or use a different layout in the
same prefix. Provisioning completes before any worker starts.

The local Floci S3 implementation supports ETag-conditional PutObject with If-Match on
existing objects. Its rejected conditional PUT leaves its connection unusable: after
412, close and replace that contender's client before another S3 request. Give concurrent
contenders independent clients. The supplied Storage helper provides a client per thread
and reset_client() for this purpose; it does not implement a storage transaction.
If-None-Match create-only puts and conditional CopyObject are unsupported in this local
venue. This transport limitation does not alter the delivery contract.
