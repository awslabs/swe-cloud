# Delayed-task runner: work lost for the length of a hiding window

We run a small delayed-task service: producers publish a task with an absolute
`due_at`, a worker takes the delivery as soon as the queue offers it, keeps the
task on its local schedule, and commits the task's effect when the time comes.
The queue is a standard SQS queue; the task rows live in DynamoDB. The program
is in `/app` (`README.md` describes it, `worker.py` is the entry point).

Three incidents have reached on-call, from the same fleet, the same queue and
the same program:

1. **Duplicate executions, last quarter.** A task scheduled far out ran twice:
   the row read `effect_count = 2`, and the two executions came from two
   workers, each with its own copy of the delivery. On-call's write-up ends
   with "raised the hiding window to 40 s, covering the longest due time we
   schedule; duplicates stopped."

2. **Work returning far too late, now.** With that change in place, a worker
   destroyed without warning — OOM kill, `kill -9` — leaves its unpublished
   tasks unrun for a long time. The most recent example: a task due 25 s after
   publication whose holder was killed 6 s in. Its effect committed 15 s late,
   when another worker finally picked the delivery up and ran it immediately.
   Nothing in that worker's logs; the schedule entry simply died with the
   process and the queue had nothing to offer anyone until the window ran out.

3. **Work run outside the holder of record.** Three times this month a
   task's effect was committed by something other than the holder the fleet
   was going by. On-call kept the worker lines, supervisor lines, host lines
   and table reads from all three cases in `/app/ops/oncall-excerpts.md`.

We want all of this gone at once, not one behaviour back in exchange for
another.

## What must hold afterwards

- Every task commits its effect **once**, **at or after its `due_at`**, and
  within 2 s of it — including a task whose holder is destroyed, and a task
  whose holder is suspended part-way and later resumed.
- When a holder is destroyed without warning, its still-unpublished task is
  **back in the queue and in another worker's hands within 8 s** of the
   holder's death. The delivery that comes back must be the one that was
   published — the supervisor checks the message id and how many times that
   message has been received.
- A task commits once **no matter how many copies of its delivery are about**,
  ending in at most one increment of that task's counter.
- The task row keeps recording **who currently holds the task**, and the
  table is right about it: a takeover is visible in the table and not only
  in process state, a task does not leave the holder the fleet currently
  has for it, and its effect is committed by that holder — not by a process
  that has already been replaced, even once that process is back.
- Workers that are not destroyed stay up.

## What must be preserved

- `/app/worker.py` stays the entry point with the command line it has now, and
  the stdin/stdout protocol in the README stays as documented — commands in,
  `READY` / `HELD` / `FIRED` / `IDLE` out. The supervisor drives workers
  through it and cannot be changed.
- A worker still takes a delivery as soon as it is offered and reports `HELD`
  for it; work due in the future is not left sitting in the queue.
- A worker receives only when told to. The local schedule survives process
  suspension: an entry that becomes due while the process is stopped is still
  dispatched when the process runs again.
- Deliveries stay in SQS until the work is done. The queue is the one the
  supervisor created, a standard queue; publishing replacements, diverting
  work to another queue, or configuring the queue to hold work back until it
  is due are not available to you.
- `effect_count` stays a counter: it is incremented, never assigned, never
  reset. A task that ran twice must be able to read as 2.
- The task row's documented fields — identity, `due_at`, holder, phase,
  counter, completion time — keep the meaning the README gives them.

## Environment

Everything runs in one container against Floci on
`http://localhost:4566` (`us-east-1`, credentials `test`/`test`). Compute is
ordinary local processes: a worker's memory is gone when it is killed, and two
workers share nothing through the filesystem. Anything that has to survive a
process, or be seen by another process, belongs in the queue or the table.

## How to run it

`README.md` has the worker command line and the producer tool. To try the
program, start a worker against a fresh queue, publish a task with `due-in`,
and watch it commit at the due time; drive a second worker with
`{"cmd": "RECEIVE"}` lines on its stdin to place work by hand.
