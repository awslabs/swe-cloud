# Persistent records

All timestamps are UTC epoch values. No table has TTL enabled. Expiry must be handled by clients;
DynamoDB background deletion is not a recovery mechanism.

The idempotency table has string hash key `id`: SHA-256 of the event encoded with
`json.dumps(event, sort_keys=True, separators=(",", ":"))` (Python's default ASCII escaping).
One item describes the current processing or cached result of that event. Operational readers
use `status` (INPROGRESS or COMPLETED), `owner_execution_id` (the execution associated with this
record, including after completion), and `in_progress_expiration` (the millisecond time until
which the unfinished execution can still resume). The redelivery router reads these fields directly from this table. `expiration`, in seconds, is the response retention
horizon: the default 3600 seconds is longer than the default execution's lifetime. It must not shorten
the protection of an unfinished execution when a longer runtime limit is supplied.

On completion, `response_data` is JSON for the receipt and remains usable until `expiration`.
Re-submitting an order with an unexpired COMPLETED item returns that receipt, regardless of the
new execution identity, and does not replace the attribution with the cache reader's identity.
An expired completed response may be processed anew. An admitted invocation owns responsibility
for finishing or cleaning up its reservation; a rejected caller must leave that item alone.

The checkpoints table uses execution_id (string hash) and seq (number range). Each entry has
kind and name, plus JSON data for a step or resume_at_ms for a wait. The effects table uses
payload_key (string hash, same event digest) and execution_id (string range), with a dispatched_ms
timestamp and a dispatch_count diagnostic. The handler adds receipt_render_count for each completed receipt rendering;
this is diagnostic callback telemetry, not an idempotency lock. Both are runtime-owned tables; the idempotency wrapper must not edit them. A dispatch
from two distinct executions is two external operations, even if a later response is cached.

The execution dispatch registry uses EXECUTION_TABLE (default durable_executions), with string
hash key execution_id and string active_invocation_id. It is runtime-owned: the wrapper may
read it or use it in conditions, but must not modify its rows. Tickets have no sortable ordering.
The scheduler replaces active_invocation_id before dispatching any replacement, including
replays after a wait. A registry row is retained throughout the execution. It identifies which
invocation may act now; owner_execution_id on the order record identifies the execution whose
work is reserved across waits and worker replacement. Replacement does not release that order
or change its absolute resume deadline. The registry and idempotency tables are in the same
AWS account and region. No local lock coordinates their writers.


Transport errors do not prove whether a mutation committed. The order record remains the
source of operational status during recovery; a lost acknowledgement must neither erase a
live reservation nor turn a single callback return into two results. Extra bookkeeping may
be added to these rows or to separate application-owned tables. Runtime-owned registry,
checkpoint and effect rows must remain untouched by the idempotency layer. An invocation
still awaiting admission must not enter the callback after revocation; a conservative
reservation for that execution is allowed if its current continuation can recover it.

Bundle identity reservations are described in README's Bundle dispatch section. A row can carry
additional state for a partially admitted bundle. If acquisition is refused, leaving an INPROGRESS
tombstone is acceptable only if its lease is already reclaimable and its shared identities are
available to other orders within the documented recovery interval. Completed response retention
is RESPONSE_RETENTION_S (3600 by default). Both ordinary cache reads and shared identity reuse
must honor that same horizon. Shared reservation bookkeeping may point to an order, but the order
can be replaced after expiry or error cleanup; an old pointer is not evidence about its replacement.
