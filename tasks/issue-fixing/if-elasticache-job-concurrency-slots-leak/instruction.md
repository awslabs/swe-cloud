# `process` jobs stall with every MaxConcurrency slot taken and nothing running

Our `process` job pools (a Python port of gocraft/work, in `/app`; start with `/app/README.md` and
`/app/docs/OPERATIONS.md`) keep wedging in production.

What on-call saw, twice this week:

- Every pool was up and heartbeating, but `work:jobs:process:lock` read 3, our MaxConcurrency, while
  one job was actually running (the first time, none). The queue kept growing until someone drained
  the pools and deleted the lock keys by hand.
- The pool logs around both incidents have bursts of
  `ERROR: worker.remove_job_from_in_progress.lrem - Connection reset by peer` while the node relay was
  being restarted. The second incident also overlapped a deploy that SIGKILLed one pod mid-job, and
  the memory guard killed another.
- When we reconciled afterwards, a few jobs had left the queue and never ran anywhere, and the monthly
  report that was running on the pod the memory guard hit had run twice: it finished there, and again
  on another pod. The partner billed both.

Make the pools keep their own accounting straight. Whatever happens to a pod or a connection,
capacity must come back by itself within the recovery objective in `docs/OPERATIONS.md`, and every
enqueued job must run, once. MaxConcurrency is a partner's hard limit and must never be exceeded, not
even for a moment. Pools have to keep running through Valkey errors.

Keep the interface the README describes: the `bin/pool` command line, its environment and its
`READY` line, the key layout and what each key holds, and running jobs through `$JOBRUN`. Other pools,
the dashboards and the reapers read those keys. Each pool process starts in its own working
directory, and nothing on local disk is shared between pools.

`/app/bin/dev-valkey` gives you a Valkey cluster to run against (see the README).
