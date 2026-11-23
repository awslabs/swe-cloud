# Incident brief: forbid broken around a scheduler replacement

We run `cadence`, a small HA job scheduler: two scheduler processes contend for
a lease in DynamoDB and the owner decides when each job is due; three worker
processes run the bodies. Jobs are configured `forbid` - one attempt of a job
at a time. All durable state is one DynamoDB table at `http://localhost:4566`
(the emulator); processes hold nothing of value in memory or on local disk, and
are expected to be killed and replaced at any moment. The contract the
processes implement, the item layout and the tuning constants are in
`/app/README.md`.

## What was seen

**1. A job ran twice.** On the night of the 28th, `import-batch` (forbid) had
an attempt running on worker `w9` from 00:55:03. At 01:00 the scheduler process
then leading (`sched-5`) fell over under load and was restarted at 01:34. At
01:05 - after the replacement had settled - the same job started a *second*
attempt on worker `w5`, while the first was still running; the two bodies ran
concurrently for several minutes. With no restart in the window, forbid holds
as configured: every later nominal of a busy job is decided `SKIPPED_BUSY`.

**2. The schedule went quiet.** The same week, on another cluster, after the
leading scheduler died and a survivor took over, the survivor logged a clean
handover and then decided nothing:

```
23:28:46 [scheduler sched-2] cluster: lease acquired epoch=54
23:28:46 [scheduler sched-2] agent: starting scheduler jobs=2
```

- no `schedule:` decision lines after that, for any armed nominal, on either
  scheduler, until the processes were restarted by hand;
- the API and the lease looked healthy the whole time; a start log was not
  evidence of a running schedule.

**3. A frozen worker carried on.** `w4` had a body of `rollup` waiting for its
callback acknowledgement when its host was frozen by the hypervisor, from
02:17:11 to 02:17:51. While it was frozen the job moved on, as it should: a later
nominal was admitted and started on `w2`. The body holder's own log:

```
02:17:43 holder: start job=rollup slot=33 worker=w2 run_id=b81f40c2  granted
02:17:46 holder: grant job=rollup slot=31 worker=w4 run_id=7c1e09d2
02:17:51 holder: end   job=rollup run_id=7c1e09d2
02:18:29 holder: end   job=rollup run_id=b81f40c2
```

All three reports involve the same window: a process that owned something is
gone or has stopped answering, another one owns it now, and work handed out
before the change is still out there.

## What must hold afterwards

- Forbid holds across a scheduler replacement. While an attempt of a job is
  running - on whichever worker, dispatched by whichever scheduler - no other
  attempt of that job may start, and the fact that it is running must survive
  the scheduler process that dispatched it. Our tooling reads the running
  record straight from the table, so the record itself has to carry this.
- A replacement must not stop the schedule. The surviving scheduler decides
  every armed nominal inside the declared window, and work whose *worker* has
  died or stopped answering must not block its job indefinitely: the job has
  to become runnable again, on a worker that is still alive, without anyone
  editing the table by hand.
- A completion that belongs to an attempt which is no longer the job's current
  attempt must not close the current attempt's running record. Completions
  from dead or superseded attempts arrive late; they must be harmless.
- Exactly one terminal decision per nominal, in order, and no nominal is ever
  replayed or backfilled. The decision ledger is what other teams reconcile
  against, so it must keep matching what actually ran.

## What must be preserved

- The process interfaces, the control messages, the body-callback handshake,
  the item layout and the tuning constants are the deployed contract: the
  launcher starts processes by those command lines, holds and releases bodies
  through that callback, and injects completions through that control path.
  Keep them exactly as they are, including the pauses the starter already
  announces (a body waits for its callback acknowledgement, however long the
  holder takes, while the worker keeps its claim maintained).
- Coordination state belongs in the state table. Local files and process
  memory are not shared between the processes that have to agree, and workers
  run in private working directories.

## Reproducing and checking

`python3 tools/standalone.py` runs one scheduler and one worker on a private
table and tails their logs; `/app/README.md` has the piece-by-piece commands,
the item layout and how to dump a partition. The fault window above is
reproduced by killing the leading scheduler (and, for the second report, doing
so while a body is still running on another worker) - the processes are meant
to be killed with SIGKILL and restarted with the same id and empty memory.
Hosts also freeze: a process can be stopped (SIGSTOP) for longer than any lease
and later continue (SIGCONT) exactly where it was.

Fix the program under `/app` so that everything in "What must hold afterwards"
is true. The whole system is yours to change; only the interfaces, the layout
and the ledger semantics above are fixed.
