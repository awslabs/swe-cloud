# dq

`dq` is our job queue: a Python port of BullMQ's job engine. All queue state lives in one
ElastiCache (Valkey) node; every state change is a Lua script under `dq/lua/`, run atomically with
EVALSHA. There is no server process of our own: producers and workers are ordinary processes that
talk to Valkey.

- **Producers** run `python3 -m dq add`, one short-lived process per job, from the web tier and from
  cron. Many run at the same time, and they run whether or not any worker is up.
- **Workers** run `python3 -m dq worker`, long-lived, one job at a time, under a supervisor that
  kills (`SIGKILL`) and replaces a worker whose health check fails.

Both find the node through `DQ_REDIS_URL` (`redis://<host>:<port>/0`).

## Adding jobs

```
python3 -m dq add --queue Q --name N [--data JSON] [--dedup-id D [--ttl-ms T | --keep-last]]
```

Prints exactly one JSON line on stdout and exits 0:

```json
{"job_id": "42", "deduplicated": false}
```

`deduplicated: false` means job `job_id` was created and is waiting. `deduplicated: true` means no
job was created and `job_id` is the existing job that the add was deduplicated against.

## Deduplication

With `--dedup-id`, at most one job per id is in flight. The web tier uses the user's email as the id
for the per-user jobs (welcome email, account sync, ...): a user must never have two of those
running at once, and repeated clicks must not pile up jobs.

**Simple mode** (no `--ttl-ms`). The id is held by its job until that job is completed or failed.
As long as the job is not finished, every add with the same id is deduplicated against it: while
it waits, while it runs, while it is being retried after a failed attempt (backoff), and when a
worker dies under it and the stall checker puts it back in the queue. Once it has completed or
failed, the next add with that id creates a new job.

**Throttle mode** (`--ttl-ms T`). The id is held for T ms from the add that created the job, and
every add with the same id in that window is deduplicated against that job. The window is a fixed
time span: it is not shortened when the job finishes, fails or is removed, and later adds don't
extend it.

**Keep-last mode** (`--keep-last`, used for account sync: the job must run at least once after
every change, with the latest data). The id is held as in simple mode, with one difference: an add
that arrives while the job holding the id is running is not thrown away. Its name and data are kept
in `dq:<queue>:dn:<dedup id>` (the add is still deduplicated against the running job), and once the
running job has completed or failed, a new job is created from the kept data and takes over the id.
If several adds arrive while the job runs, only the most recent data is kept. So:

- no two jobs with the id ever run at once;
- at most two jobs: the running one and the one created when it finishes;
- the next job always has the latest data; data that was replaced never runs.

An add while the job is waiting or delayed is deduplicated as in simple mode, and so is one while
the job is back in the queue after a stall. A failed attempt that will be retried does not finish
the job: the kept data stays until the job finally completes or fails. `--ttl-ms` is ignored with
`--keep-last`.

Every deduplicated add also appends a `deduplicated` event to `dq:<queue>:events`.

## Workers

```
python3 -m dq worker --queue Q [--handler module:function] [--lock-ms 30000]
    [--lock-renew-ms LOCK_MS/2] [--stalled-interval-ms 30000] [--max-stalled-count 1]
    [--attempts 1] [--backoff-ms 1000]
```

Once connected, with its stall checker running, a worker prints `READY <pid>` on stdout; the
supervisor waits for that line before it counts the worker as up. Logs go to stderr.

- **Locks.** A worker takes a job by moving it from wait to active and setting
  `dq:<queue>:job:<id>:lock` to its token with a `--lock-ms` expiry. While the handler runs, the
  lock is extended every `--lock-renew-ms`. A handler that hangs in a live worker keeps its job.
- **Stalls.** Every worker runs the stall checker every `--stalled-interval-ms` (one pass per
  interval across all workers). A pass marks every active job; a job still marked on the next pass
  whose lock has expired lost its worker. It goes back to wait and its `stc` count goes up; a job
  that stalls more than `--max-stalled-count` times is failed with "job stalled more than allowable
  limit" and is not retried.
- **Retries.** A handler that raises is a failed attempt. The job is retried after
  `--backoff-ms * 2**(attempts made - 1)` ms in the delayed set, until `--attempts` attempts have
  been made; then it is failed. A stall is not an attempt.
- Finished jobs (completed and failed) are kept.

## Handlers

`--handler module:function` (default `handlers:run`, the example in `handlers.py`) is imported from
`PYTHONPATH`. It is called with one dict:

```python
{"id": "42", "name": "welcome-email", "data": {...}, "dedup_id": "ana@example.com", "attempt": 1}
```

Its return value (JSON-serialisable) is stored as the job's `returnvalue`; an exception is a failed
attempt.

## Key layout

Ops tooling, dashboards and the on-call runbook read these keys directly; keep them.

| Key | Type | |
|---|---|---|
| `dq:<queue>:id` | string | job id counter |
| `dq:<queue>:job:<id>` | hash | `name`, `data`, `deid` (dedup id), `state` (`wait`, `active`, `delayed`, `completed`, `failed`), `atm` (attempts made), `stc` (stall count), `timestamp`, `processedOn`, `finishedOn`, `returnvalue`, `failedReason` |
| `dq:<queue>:job:<id>:lock` | string | worker token, with expiry |
| `dq:<queue>:de:<dedup id>` | string | id of the job holding the dedup id; expires only in throttle mode |
| `dq:<queue>:dn:<dedup id>` | hash | keep-last: `name`, `data`, `timestamp` of the next job |
| `dq:<queue>:wait`, `dq:<queue>:active` | list | job ids |
| `dq:<queue>:delayed` | zset | job ids by due time (ms) |
| `dq:<queue>:completed`, `dq:<queue>:failed` | zset | job ids by finish time (ms) |
| `dq:<queue>:stalled`, `dq:<queue>:stalled-check` | set, string | stall checker state |
| `dq:<queue>:events` | stream | queue events |

## Running it locally

```
eval "$(python3 tools/dev_cluster.py)"     # a fresh Valkey node; exports DQ_REDIS_URL
python3 -m dq worker --queue demo &
python3 -m dq add --queue demo --name welcome-email --data '{"seconds": 1}' --dedup-id ana@example.com
```
