# Operating the `process` pools

## Topology

- Four to six pods, each running one pool (`bin/pool`). A pod gets a fresh pool id every time its
  process starts; nothing on a pod's local disk outlives the process or is shared with other pods.
- Valkey is a single ElastiCache node. Pods do not connect to it directly: every node runs a small TCP
  relay, and `VALKEY_HOST`/`VALKEY_PORT` point the pool at its node's relay.
- Current settings: `MAX_CONCURRENCY=3`, `HEARTBEAT_PERIOD_S=1`, `DEAD_TIME_S=4`, `REAP_PERIOD_S=1`,
  `REAP_JITTER_S=1`. (gocraft/work's defaults reap every ten minutes; we shortened them, as the
  reporter of gocraft/work#192 did, so a dead pod's work comes back in seconds.)

## What the pools have to live through

- **SIGKILL.** Deploys that run out of grace time kill a pod's whole process group with no cleanup.
  Its heartbeat is left behind; the reapers in the surviving pools notice it going stale, put its
  in-progress jobs back on the queue and remove the pool.
- **Memory guard.** When a pod goes over its memory limit, the node's memory guard SIGKILLs the
  largest process in the pod, which is always the pool process (`bin/pool`; runners are small).
  Nothing else in the pod gets a signal: whatever the pool had started keeps running, and its runners
  finish their jobs. A pod whose pool process is gone is replaced at the next deploy.
- **Connection resets.** The node relay is restarted several times a day and also resets connections
  it decides are stuck. A reset can land at any point in any request a pool makes: before Valkey has
  read the command, or after Valkey has executed it and before the reply has reached the pool. The pool
  sees the same `ConnectionError` either way; the next command opens a new connection through the
  relay. Valkey's side of this is in `docs/valkey/transactions.md` and `docs/valkey/eval.md`. A relay
  restart takes a few hundred milliseconds, well inside `DEAD_TIME_S`, and pods are never cut off from
  Valkey for longer: a pool whose heartbeat goes stale is one whose pool process is gone.
- **No restarts on error.** Nothing restarts a pool that exits; it stays down until the next deploy.

## Limits and objectives

- **MaxConcurrency is a hard limit.** Every `process` job holds a session with our partner for as long
  as its runner process lives (README, "Jobs"). The partner allows three concurrent sessions per
  account; a fourth gets the account throttled for an hour. Never more than three `process` jobs may
  be executing at once, not even for a moment.
- **Recovery objective.** Capacity that no executing job holds — a slot of a pool that died, or one
  left behind by an interrupted request — must be usable again within 10 seconds, with nobody touching
  keys, while there is work waiting.
- **Every job runs, once.** Enqueued jobs are customer requests; one that leaves the queue must run.
  Nothing but a deploy may cut a runner off mid-job, and only a job a deploy cut off runs a second
  time (README, "Jobs").
- Jobs take from about 300 ms to several minutes; the monthly reports run for three to four minutes.

## Manual recovery (what on-call has been doing)

With every pool drained: `DEL work:jobs:process:lock work:jobs:process:lock_info`, then delete each
pool's `work:jobs:process:<ID>:inprogress` after pushing its entries back onto the queue. Never do this
while jobs are executing: the counter comes back as 0 while those jobs still hold partner sessions,
and the pools admit three more next to them (the gocraft/work maintainers make the same point in
gocraft/work#55: "We can't just _reset_ the lock val because there may be active jobs running").
