# Incident 4831 — recovery is undoing settled transactions and stalling on others

## The service

`/app` runs a small multi-item transaction service on DynamoDB. A coordinator
records the whole intent in a transaction record before touching any
participant, locks each participant, saves its previous value as an image,
applies the write, then decides the record COMMITTED or ROLLED_BACK and cleans
up. A sweeper periodically picks up records their coordinator abandoned: it
decides old pending ones and finishes the cleanup of decided ones. Three
tables hold everything — the transaction records, the participant items, and
the saved images — and every rule the service has is a write against them, so
a transaction one process started can be finished by another.

The service is pointed at the Floci endpoint in the usual environment
variables (`AWS_ENDPOINT_URL`, `us-east-1`, `test`/`test`).

## What was seen

Tuesday night the host running several coordinators died hard (SIGKILL, no
shutdown hooks), with transactions in flight. The sweeper did its usual passes
afterwards, and completed records were reaped at the retention time as usual.
By Wednesday morning the audit found, across the item table, the image table
and the record table:

1. A transaction the on-call had *verified as COMMITTED* during the incident —
   they read the record and confirmed the new values and the locks it still
   held — read `ROLLED_BACK` later that morning, and every value it wrote was
   back at its pre-transaction baseline.
2. A client was holding a success acknowledgement for a write committed *after*
   the crash. By the next pass the item showed its old value again.
3. One abandoned transaction never finished: its record still has no completion
   mark, one of its saved images is still lying around, and it went through at
   least two more sweeper passes without progressing.
4. One participant refuses every write: it carries a lock marker naming a
   transaction the record table no longer has — that record was completed and
   reaped — and a saved image for that same transaction is lying around with no
   record left to account for it.

Nothing outside this program writes to these tables.

## What must hold afterwards

1. Once a record durably shows COMMITTED or ROLLED_BACK, later recovery
   activity never flips it to the opposite terminal state and never undoes the
   writes that decision applied — recovery finishes that record the way it
   decided instead.
2. A write acknowledged as successful to a client stays visible after any later
   recovery pass.
3. A pending record whose coordinator is gone reaches a terminal decision and
   full completion — locks released, images reclaimed, completion mark set —
   within the recovery window (15 s after interference stops), at the
   configured pending-age threshold of 2000 ms. That threshold, the retention
   time and the recovery window are operating parameters: fixing this by
   raising them, or by declining to intervene, is not a fix.
4. Recovery is itself crash-prone, and so are the writers it cleans up after.
   Whatever a killed process leaves behind, a later pass finishes, without the
   original process and without any process memory surviving.
5. A decision authorizes exactly the request set the decider verified. A commit
   attempt that raced with an appended request fails rather than committing the
   unverified set, and the transaction still completes with its full verified
   set once the concurrent append has been applied.
6. The tables come back to a state a fresh transaction can use: no participant
   keeps refusing writes, and no saved image survives that the record table
   cannot account for — including after the record that ordered it has been
   reaped at the retention time.

## What is preserved

- The three-table layout, attribute names and record shapes; the operations
  dashboards read them directly.
- The command line, the JSON-line command interface, and the pauses the program
  announces while it works. The control plane that drives and grades this
  service relies on both; a build that stops announcing them, or announces
  them somewhere else, is broken however correct its writes look.
- All service access goes through `/app/db.py`: single-item reads and writes,
  scan, table lifecycle. Multi-item transactional, batch and statement APIs are
  outside the access contract — the durability argument of this service is
  built on single-item conditional writes, and the contract is enforced before
  dispatch.
- Coordinators and sweepers run as separate processes with no shared local
  state; anything that must survive a process death belongs in the tables, not
  in a file or a process's memory.

## Running it

Tables are created on demand. A coordinator, fed one JSON command per line:

```
python3 /app/txnmgr.py coordinator --tx-table tx --item-table items --image-table images
{"cmd": "begin", "tx": "t1"}
{"cmd": "append", "tx": "t1", "item": "item-1", "val": 7}
{"cmd": "apply", "tx": "t1", "item": "item-1"}
{"cmd": "commit", "tx": "t1"}
```

A sweeper over the same tables:

```
python3 /app/txnmgr.py sweeper --tx-table tx --item-table items --image-table images \
    --rollback-after-ms 2000 --retention-ms 2000 --sweep-interval-ms 100
{"cmd": "sweep", "tx": "t1"}
{"cmd": "run"}
```

Each command prints one JSON response. `commit` answers `COMMITTED` or
`ROLLED_BACK` only after the record has been decided and fully cleaned up, so
an acknowledged `COMMITTED` means the values are in place and the locks are
gone. The items must exist before they are appended (seed them with a plain
`PutItem`). Reproduce the incident by killing a coordinator mid-transaction
(`kill -9`) and letting the sweeper run; the audit items above all came off
schedules that leave a process parked at one of its announced pauses while the
rest of the system moves on without it.

Fix the program under `/app` so the checklist above holds. The graded verifier
rebuilds the incident schedule against the tables and the announced pauses and
reads the DynamoDB state itself.
