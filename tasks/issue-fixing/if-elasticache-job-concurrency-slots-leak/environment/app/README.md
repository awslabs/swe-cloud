# work: the `process` job pools

A Python port of [gocraft/work](https://github.com/gocraft/work) (Go, at 5959e69) for our one job type,
`process`. It keeps gocraft/work's Valkey key layout and Lua scripts byte for byte, so the Go web UI,
the dashboards and the on-call scripts read the same keys they always did. Production details are in
`docs/OPERATIONS.md`; Valkey's own documentation for the commands we lean on is in `docs/valkey/`.

```
bin/pool        run one worker pool until SIGTERM
bin/enqueue     push jobs the way the producers do
bin/jobrun      local stand-in for the production job runner (sleeps for the job's duration)
bin/dev-valkey  create or reuse a development Valkey cluster through ElastiCache
work/           the pool: worker.py, reaper.py, heartbeater.py, pool.py, scripts.py (Lua), keys.py
```

## Running a pool

```
/app/bin/pool --pool-id <ID> --workers <N>
```

| variable | meaning | default |
|---|---|---|
| `VALKEY_HOST`, `VALKEY_PORT` | where the pool connects (in production, the node's relay) | `localhost`, `6379` |
| `NAMESPACE` | key namespace | `work` |
| `JOB_TYPE` | the job type this pool runs | `process` |
| `MAX_CONCURRENCY` | written to `<ns>:jobs:<type>:max_concurrency` at start | `0` (no cap) |
| `HEARTBEAT_PERIOD_S` | heartbeat period | `5` |
| `DEAD_TIME_S` | heartbeat age after which a pool counts as dead | `10` |
| `REAP_PERIOD_S`, `REAP_JITTER_S` | dead pool reaper period, plus a random 0..jitter-1 whole seconds | `600`, `30` |
| `JOBRUN` | the job runner executable | `/app/bin/jobrun` |

The pool writes `max_concurrency`, writes its first heartbeat, prints `READY <ID>` on stdout, and
then runs its workers, its heartbeater and its dead pool reaper until SIGTERM or SIGINT. Pool ids are
unique per process start. Errors are logged to stderr as `ERROR: <where> - <error>`.

## Jobs

Producers `LPUSH` the job JSON onto `<ns>:jobs:<type>`:

```json
{"name":"process","id":"<id>","t":<enqueued at, epoch s>,"args":{"duration_ms":<n>}}
```

A worker runs a job by starting `$JOBRUN --job-id <id> --pool-id <ID> --duration-ms <n>` as a child
process and waiting for it to exit. In production the runner opens a session with our partner, does
the work and closes the session when it exits, so a job holds a partner session for exactly as long as
its runner process lives. Jobs take anything from a few hundred milliseconds to several minutes.

A job runs once, start to finish. The partner bills every run, and a run that is cut off has already
made calls that cannot be taken back. The one exception is a job whose runner was killed together with
its pod (see "SIGKILL" in `docs/OPERATIONS.md`): the reaper puts it back on the queue and it runs again.

## Keys (namespace `work`, type `process`)

| key | type | holds |
|---|---|---|
| `work:jobs:process` | list | the queue; producers `LPUSH`, workers take from the other end |
| `work:jobs:process:<ID>:inprogress` | list | the raw JSON, exactly as enqueued, of every job pool `<ID>` has claimed and not finished |
| `work:jobs:process:lock` | string (int) | the number of `process` jobs claimed and not finished, across all pools |
| `work:jobs:process:lock_info` | hash | per pool id: that pool's share of `lock` |
| `work:jobs:process:max_concurrency` | string (int) | MaxConcurrency, written by every pool at start |
| `work:jobs:process:paused` | string | present while the type is paused (we never pause it) |
| `work:worker_pools` | set | ids of pools that have heartbeated and were not reaped or stopped |
| `work:worker_pools:<ID>` | hash | the pool's heartbeat: `heartbeat_at`, `job_names`, `concurrency`, ... (no TTL) |

`lock` is the admission gate: the fetch script (`scripts.FETCH_JOB`, run atomically by Valkey) claims a
job only while `lock` is missing or below `max_concurrency`, and a claim is three records made
together: `lock` +1, `lock_info[<ID>]` +1, and the job's raw JSON moved from the queue into the pool's
in-progress list. Finishing a job takes all three back: the raw JSON comes out of the in-progress list
and the pool's unit is given back. So at any moment, for every pool,
`lock_info[<ID>]` = length of `<ID>:inprogress`, and `lock` = the sum over pools.

Every pool runs a dead pool reaper (`work/reaper.py`). A pool whose heartbeat is older than
`DEAD_TIME_S` is dead: the reaper moves its in-progress jobs back onto the queue, giving back one unit
for each, deletes its heartbeat, removes it from `work:worker_pools` and drops its `lock_info` field. A
pool that stops on SIGTERM removes its own heartbeat and pool entry.

## Local development

```
eval "$(/app/bin/dev-valkey)"                       # VALKEY_HOST / VALKEY_PORT for the commands below
MAX_CONCURRENCY=3 /app/bin/pool --pool-id dev-1 --workers 4 &
/app/bin/enqueue --count 20 --duration-ms 400
```

## What other systems rely on

The `bin/pool` command line, its environment and its `READY <ID>` line; the key layout above and what
each key holds (the dashboards, the Go web UI and every other pool's reaper read them directly);
running every job through `$JOBRUN`.
