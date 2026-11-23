# Delayed-task runner

A small fleet of worker processes that run queued tasks at a future time.
Producers put a task on the queue with an absolute `due_at`; a worker takes
the delivery as soon as the queue offers it, keeps the task on its local
schedule until `due_at`, and commits the task's effect when the time comes.

State lives in two places and only those two places:

* **SQS** — the queue holds every task that has not run yet. A delivery is
  removed only when its task runs.
* **DynamoDB** — one row per task identity: what it is, who holds it, how
  many times its effect has been committed.

A worker process holds nothing that survives it. Its local schedule is in
memory: entries due while the process is suspended still fire when it runs
again, and entries held by a process that dies are gone with it.

## Layout

| file | role |
|---|---|
| `worker.py` | the worker process; entry point |
| `timer.py` | the in-process schedule of future callbacks |
| `broker.py` | queue access (receive, hide, remove) |
| `store.py` | task rows in DynamoDB |
| `settings.py` | operating configuration |
| `tools/send_task.py` | publish one task with a due time |
| `ops/oncall-excerpts.md` | raw lines kept by on-call |

## Running a worker

```
python3 /app/worker.py --worker-id A \
    --queue-url https://localhost:4566/000000000000/tasks \
    --table tasks --run-id demo \
    --endpoint http://localhost:4566 --region us-east-1
```

Publish a task and watch it run:

```
python3 /app/tools/send_task.py --queue-url <url> --task-id t1 --due-in 10 \
    --run-id demo
```

The worker prints one JSON line per event: `READY` at startup, `HELD` when it
takes a delivery, `FIRED` when a task's effect commits.

## Control protocol

A worker is driven over stdin, one JSON object per line. This is how the
fleet supervisor places work and how a standby is handed a delivery.

| command | meaning |
|---|---|
| `{"cmd": "RECEIVE"}` | attempt one receive now; the worker answers `HELD` or `IDLE` |
| `{"cmd": "ADOPT", "message": {...}}` | take this delivery as if received from the queue; answers `HELD` |
| `{"cmd": "POLL", "enable": true}` | keep receiving on its own |
| `{"cmd": "STOP"}` | exit |

`ADOPT` carries a raw queue delivery — `MessageId`, `Body`, `ReceiptHandle`
and its attributes — exactly as a receive returned it.

The supervisor also keeps the fleet's shape: a worker it can no longer
reach is replaced by a new process started under the same `--worker-id`,
and the replacement takes work like any other worker.

Events out, one JSON line each:

| event | meaning |
|---|---|
| `READY` | the worker is up |
| `HELD` | a delivery was taken; carries `task_id`, `message_id`, `receipt` |
| `FIRED` | a held task's effect committed; carries `task_id` |
| `IDLE` | a `RECEIVE` found nothing |

Any reimplementation keeps this protocol and these events, keeps
`/app/worker.py` as the entry point with this command line, and keeps the
pauses the supervisor relies on: a worker only receives when told to, and
never blocks the loop on one queue call.

## Task rows

Key `pk = RUN#<run>#TASK#<task_id>`.

| field | meaning |
|---|---|
| `task_id`, `message_id` | task identity and the queue message it came from |
| `due_at` | absolute epoch seconds |
| `holder` | the worker currently holding the task |
| `phase` | `PENDING`, then `DONE` once the effect has committed |
| `effect_count` | how many times the effect has committed — a counter, never reset |
| `done_at` | when the effect committed |

The counter is the accounting of record for "this task ran": a task that ran
twice reads `2`. Nothing in the write path may replace the counter with a
value rather than an increment.

## Configuration

`settings.py` holds the operating configuration. `VISIBILITY_S` is the number
of seconds a delivery stays hidden after this worker takes it; the queue's
own default is 4.

## Operating history

Three behaviours have reached on-call:

1. **Duplicate executions.** Tasks scheduled far out were executed twice:
   the same `task_id` reaching `effect_count` 2, with two workers each
   holding their own copy of the delivery and both callbacks running at the
   due time. This stopped when `VISIBILITY_S` was raised from the queue
   default of 4 to 40, covering the longest due time we schedule.
2. **Late work after a hard kill.** Since that change, a worker destroyed
   without warning (OOM kill, `kill -9`) leaves its tasks unrun for the rest
   of the hiding window. On-call's most recent example: a task due 25
   seconds out whose holder was killed 6 seconds in committed 15 seconds
   late, after another worker finally picked the delivery up.
3. **Work run outside the holder of record.** Three cases this month, all
   with `VISIBILITY_S = 40`: a task's effect committed by something other
   than the holder the fleet was going by. The raw lines — worker events,
   supervisor and host lines, and consistent reads of the row — are in
   `ops/oncall-excerpts.md`.

All three reports are from the same fleet, same queue, same program. The
queue's documentation is explicit that a standard queue can deliver a
message more than once, including while an earlier copy is still hidden.
