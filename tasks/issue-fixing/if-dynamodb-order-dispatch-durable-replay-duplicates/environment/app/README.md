# Dispatch service

This is the local deployment of the order dispatch handler. DynamoDB is at
http://localhost:4566, region us-east-1, credentials test/test. Start it with
`bash /usr/local/bin/start_floci.sh` if needed. All dependencies are already installed.

Run one order with:

    python /app/service.py submit --event '{"order":"ord-274","warehouse":"east"}'

The command creates development tables if needed and drives the normal execution to completion.
It prints each invocation's result. Repeating the same event should return the cached receipt.
`python /app/service.py --help` describes the command interface. No runtime state is kept in
working directories. Table names can be set with IDEMPOTENCY_TABLE, CHECKPOINT_TABLE,
EFFECTS_TABLE and EXECUTION_TABLE; defaults are idempotency, durable_checkpoints, effects
and durable_executions.

An order dispatches in a durable step, waits for the warehouse window, then finalizes a receipt.
`runtime.py` implements the context; `invoke.py` runs one invocation, and `service.py` is the
local scheduler. The supported idempotency integration is `idempotency.idempotent(handler)`
and `idempotency.AlreadyInProgress`. It wraps a `handler(event, ctx)` returning a JSON object.
A conflicting submission reports FAILED with error AlreadyInProgress without entering the
handler. A valid cached response reports SUCCEEDED without entering the handler.

## Runtime lifecycle

An execution has one identity and an absolute start time assigned when it is accepted. Dispatch
may be delayed. It may run many invocations, each in a new process. A suspension ends that
invocation without completing the execution. Replays rebuild completed operations from DynamoDB;
step callbacks for those operations are not repeated. A killed invocation may be retried under
the same execution identity even if it wrote no checkpoint. Such an invocation has EXECUTION
mode and operations_count=1. Mode reflects the checkpoint history loaded for this invocation.
Redelivery from the queue creates a new execution identity even for an identical event.

The invocation timeout is a process resource limit. Execution timeout bounds the entire execution,
including dispatch delay, waits and backoff, measured from acceptance, not first invocation.
The runtime never dispatches or resumes an execution at or after its execution deadline. Once
that deadline passes, an unfinished order must be available for a new execution within three
seconds.
Worker recycling is cooperative: a replacement can be dispatched while a previous worker is
still returning from a callback. Before dispatch, the scheduler replaces the execution's active
invocation ticket in DynamoDB. That write revokes the previous ticket immediately, including
when the replacement worker has not reached the decorator yet. An invocation whose admission
has not yet committed must be refused after revocation. If admission has returned only
transport errors before revocation, recovery must establish current authority before
starting the callback; an earlier committed request by itself is insufficient. An admitted worker must not publish its return
value as a successful receipt or remove the reservation after its ticket is revoked.
In particular, an error from that worker must not release a live execution's order to redelivery.
DynamoDB requests already sent by a worker may arrive after the registry update; workers
and the scheduler use independent connections. The current invocation of the same execution
must still be able to continue. A worker already in the handler may unwind; its error still
propagates. Revocation has no effect on a receipt committed before it. Returning an existing cached receipt remains safe.

workers.py issues tickets and requests cooperative retirement when a process exceeds its
invocation limit. Retired processes are reaped on later scheduler turns after a three-second
grace. The execution deadline still bounds all attempts.

The scheduler owns the dispatch registry. Each invocation gets an opaque, unique invocation_id;
it stays fixed inside that process. runtime.py checks the active ticket at step/wait boundaries,
but the decorator owns admission and all idempotency-record mutations. Checkpoint checks do not
cover a callback returning or failing after replacement. No crash-after-side-effect/before-checkpoint
guarantee is requested in this deployment.

`ctx` supplies execution_id, invocation_id, execution_start_ms, execution_timeout_ms, mode, operations_count,
get_remaining_time_in_millis(), step(name, callback) and wait(seconds). Start and timeout remain
constant across invocations of one execution. `ctx.wait()` suspends by raising a BaseException;
it is not a handler failure and must retain the reservation. Other handler exceptions propagate
after cleanup. The default local limits are 4 seconds per invocation, 45 seconds per execution,
and 15 seconds waiting; requests may supply different execution limits.

The table contract used by support tooling is in [docs/storage.md](docs/storage.md).

## Gateway recovery

The service can route DynamoDB traffic through `egress.py`. A recent resiliency change added
`retry.py` and retries around the decorator; receipts now also carry a per-return delivery_id.
The normal direct-endpoint path remains useful for smoke tests. To run through the gateway,
start `python /app/egress.py` in a separate terminal and set
`AWS_ENDPOINT_URL=http://localhost:4567` for the submission. `egress.py --help` includes short
outage controls for development. Its upstream stays at port 4566. Only the worker uses this
connection; the scheduler may still rotate tickets on a healthy independent connection.

The gateway can lose a successful mutation response or fail before forwarding the request.
Both look like a transient InternalServerError to the worker. Short bursts can outlast one SDK
call; supported recovery includes two consecutive failed calls while the invocation still has
several seconds left. The SDK is configured without its own retries here to keep the application
policy inside the invocation budget. Reads remain available during these gateway write outages.
The caller must recover an unambiguous outcome after the gateway recovers, without a process
restart or reporting a healthy order as failed solely because its write response was lost.

A callback is executed at most once in one decorator call. Preserve its first return value
through completion recovery: it can contain nondeterministic delivery metadata. Retrying a
storage operation is different from rerunning the callback. A callback can itself throw a
botocore exception from its downstream service; that remains a handler failure, propagates to
the caller and triggers the usual reservation cleanup. Suspension still retains the order.
When cleanup encounters a short gateway outage, it must finish recovering or recognize that
it has lost authority, while preserving the original handler error.

Ticket replacement can occur while the worker is waiting for a lost response. Recovering a
completion that committed before replacement is safe and should return the committed receipt.
Recovering an admission is different: an earlier successful write does not establish that the
worker is allowed to start its callback now. A current continuation must still be able to use
the reservation. If completion never committed before replacement, recovery cannot publish the
retired callback's result. Recovery must also preserve any result the successor has committed.

The local DynamoDB transaction API notes are in [docs/dynamodb.md](docs/dynamodb.md).

## Bundle dispatch

A submission can contain `dispatch_keys`, a list of shared delivery identities, for example:

    python /app/service.py submit --event '{"order":"bundle-7","dispatch_keys":["east:17","east:18"]}'

These identities are assigned upstream to physical deliveries. Two different order payloads may
include the same delivery. The handler dispatches the whole bundle as one operation; entering it
with only part of the bundle reserved can dispatch another order's delivery. All of its identities
must be reserved before the handler is entered. A bundle may have up to 512 identities (nonempty
strings of at most 128 bytes). Duplicates in a list refer to the same delivery. The event digest
and its completed receipt still identify the entire JSON event, including list order and metadata.
An overlap with a different event is a conflict, never a cache hit for that other event's receipt.
Events without dispatch_keys keep their existing behavior.

Reservations cover waits, killed-before-checkpoint retries and worker replacement. Disjoint bundles
must continue while another bundle is suspended. A conflict found during admission must return
AlreadyInProgress without entering the handler, and the unsuccessful admission must make its other
identities available within three seconds; it must not hold them until its execution deadline.
An interrupted admission may have reserved only part of a bundle. Its current continuation must
be able to recover, while redelivery cannot take those identities before the execution deadline.
An acknowledgement recovered after the execution deadline cannot admit a callback into a bundle
whose identities have become reusable. If the execution is abandoned, all of its identities
must be available within three seconds after
that deadline. An ordinary handler failure releases the whole bundle under the same recovery rule.
A rejected or retired worker must not release another live reservation.

A completed bundle protects its identities for the receipt's retention period, even if the
execution's resume deadline is earlier. During that period an identical event returns its cached
receipt and a different overlapping event conflicts. Once the response expires, its identities
may be reused. Reuse must not allow a delayed completion or cleanup from the previous reservation
to change the new reservation or revive the old cached receipt. A worker whose bundle reservation
was reclaimed must report AlreadyInProgress unless its receipt was already published before
reclamation. Admission can be interrupted
between durable writes; the gateway's outage modes and ticket replacement apply to bundle writes
as well as the ordinary order record.

DISPATCH_TABLE (default dispatch_reservations) is an application-owned DynamoDB table with string
hash key id, provided for shared reservation state. It has no TTL. Its contents and any extra
application bookkeeping are implementation details; the ordinary order row remains the support
team's source of status and response attribution. service.py initializes this table with the
other development tables. No cross-process file lock or in-memory map can reserve a delivery.
RESPONSE_RETENTION_S sets completed response retention, in whole seconds (default 3600); it does
not change an unfinished execution's absolute deadline. Support uses shorter retention in local
rollover exercises. Use an invocation budget large enough for a 512-identity bundle; local
service.py accepts --invocation-timeout for this purpose.
