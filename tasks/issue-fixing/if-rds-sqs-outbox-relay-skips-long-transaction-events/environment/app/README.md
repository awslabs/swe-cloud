# ledger outbox relay

The ledger services write their domain events into the `outbox` table in the same transaction as
the business change. The relay (`relay.py`) reads committed outbox rows and publishes them to the
`ledger-events` SQS FIFO queue. Downstream consumers (billing, statements, the audit trail) apply
events strictly in `position` order and treat a skipped position as "never happened".

```
 producers (app_writer) ──INSERT──▶ outbox (RDS PostgreSQL 16) ◀──SELECT── relay (outbox_relay) ──▶ ledger-events.fifo
                                    relay_checkpoint            ◀──UPDATE──┘
```

## Layout

| path | what |
|---|---|
| `relay.py` | entrypoint: connect, read checkpoint, print `READY`, poll / publish / checkpoint loop |
| `outbox_relay/frontier.py` | decides how far the relay may move on each poll |
| `outbox_relay/store.py` | outbox read, checkpoint read/advance |
| `outbox_relay/publisher.py` | SQS FIFO publisher |
| `config/relay.toml` | relay settings as deployed |
| `outbox/writer.py` | the producer library every service uses |
| `db/schema.sql`, `db/roles.sql` | schema and grants (platform team; applied as shipped) |
| `ops/devstack.py` | dev stack on the local AWS endpoint: RDS instance, schema, roles, queue |
| `tools/emit.py` | emit test events through the producer library |

## Contracts

**Producers.** Every producer goes through `outbox/writer.py`. Its first statement in every
transaction is `SELECT pg_current_xact_id()`, before any insert; positions are drawn only by the
`INSERT`'s column default, never by a direct `nextval()`. Inserts are
`ON CONFLICT (message_id) DO NOTHING`: a producer retrying a message it already wrote is dropped.
Producer transactions roll back for ordinary reasons (validation, deadlocks, timeouts).

**Publish.** One SQS message per committed outbox row with a position above the checkpoint the
relay was deployed with: `MessageGroupId` = `outbox` (a single group), `MessageDeduplicationId` =
`message_id`, `MessageBody` = the row's `data`, message attributes `position` (Number),
`message_id` and `message_type` (String). Messages go out in strictly increasing position order.
After a crash the relay may send a message again; the deduplication id makes that harmless within
the queue's five-minute window, and consumers tolerate the occasional repeat. A message sent out of
order, or never sent, is not tolerated.

**Checkpoint.** `relay_checkpoint.position = F` means: every committed row with a position `<= F`
has been sent, and every position `<= F` without a row will never have one. It only moves forward
(the `UPDATE ... WHERE position <= new` in `store.py`). It is how far the relay resumes from after a
restart, and `max(outbox.position) - F` is the relay-lag panel on the ledger dashboard, which is
expected to sit near zero whenever producers are quiet. `relay_checkpoint.meta` is free-form JSON
for the relay's own bookkeeping. Rows at or below the checkpoint the relay starts from were
delivered long ago and must not be sent again.

**Delivery latency.** A committed row reaches the queue within 5 seconds, unless an earlier
position is still being written, or the relay cannot yet rule out that an open transaction is
writing it (then it waits for that one, by the ordering rule).

## Who else is on the database

- **ledger-api** and the other online producers: short transactions, a few tens of milliseconds,
  several in flight at any moment during the day.
- **ledger-scheduler**: elects its leader with `pg_advisory_xact_lock(51080001)` on a dedicated
  connection (role `app_writer`) and keeps that transaction open for as long as it is leader, which
  is days. That session never writes; the scheduler's jobs run on other connections. It shows in
  `pg_stat_activity` as `idle in transaction`.
- **reconciliation** (month-end): writes its `ReconciliationStarted` outbox row at the start of its
  transaction, then keeps the transaction open, mostly idle, while it waits on the bank's
  statement API. That takes anywhere from seconds to a few minutes before it commits.

## Running the relay

The relay runs as `outbox_relay` with the grants in `db/roles.sql`: it can read the outbox and the
sequence, read and update `relay_checkpoint`, and read `pg_stat_*` views for every role. It cannot
write the outbox, change the schema, or signal other sessions.

In production it runs as one instance under a supervisor that starts it as `python3 /app/relay.py`
with the environment described in `relay.py`, waits for `READY` on stdout, and restarts it with the
same command whenever it exits. Deploys, evictions and node drains stop it with SIGKILL, not
SIGTERM, at any point of its loop, and the replacement is scheduled on another node: nothing it left
on local disk or in its working directory is there afterwards.

Dev loop:

```bash
eval "$(python3 /app/ops/devstack.py up)"            # RDS instance + schema + queue (first run ~1 min)
python3 /app/relay.py                                # as outbox_relay
( eval "$(python3 /app/ops/devstack.py env --writer)"; python3 /app/tools/emit.py --count 500 --writers 4 )
python3 /app/ops/devstack.py reset                   # start over
```
