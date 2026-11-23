# Flow orchestrator

The orchestration server for our flow platform. It keeps flow runs, their state history and the
deployment concurrency counters in Postgres (RDS), and the concurrency leases in Valkey
(ElastiCache). The client side — workers, `flow-run execute` supervisors and flow engines — talks
to it over a small HTTP API.

```
orchestrator/   the server (this team owns it)
  server.py       HTTP API: set_state, lease renew, run read
  policy.py       orchestration rules applied to every proposed state
  slots.py        concurrency_limit_v2 counters
  leases.py       lease records in Valkey (prefect-redis layout, Lua scripts)
  repossessor.py  returns the slots of expired leases
  schema.sql      tables, applied on start-up
  admin.py        operator tooling        devstack.py   dev RDS + ElastiCache on the AWS endpoint
  pause.py        pause points for the fault-injection rig
fleet/          the deployed client side (worker, supervisor, engine)
```

`fleet/` is the client release our workers run, pinned fleet-wide. It is here so you can run the
whole system; it is not changed by server work, and anything the server does has to work with it
as it is.

## Runs, states, retries

A run moves SCHEDULED -> PENDING -> RUNNING -> a terminal state (COMPLETED, FAILED, CRASHED,
CANCELLED). Every proposal goes through `POST /flow_runs/<id>/set_state`; the rules in `policy.py`
accept it, reject it and substitute another state (the response says REJECT and carries the state
the run actually took), or abort it. `force: true` skips the core rules (operators, sweepers).

A flow with `retries` retries in its own process: when the engine reports FAILED and retries are
left, the server rejects the failure into `AwaitingRetry` (a SCHEDULED state due `retry_delay_s`
later); the engine waits for it and proposes RUNNING again. `run_count` counts RUNNING entries.

A supervisor that receives SIGTERM (node drain, deploys) reschedules its run: it proposes
`AwaitingRetry` due now, kills its engine and exits, and a worker picks the run up again.

## Deployment concurrency

A deployment with a limit has one `concurrency_limit_v2` row; `active_slots` is the number of its
runs holding a slot, and the limit caps how many of its runs execute at once. A run takes a slot
when the worker moves it to PENDING (before anything is started) and holds it until its process
gives it up: a terminal state or a reschedule gives it back in that same request. If the process
dies without reporting anything, the slot comes back when its lease expires.

The lease (`prefect:concurrency:lease:<id>` plus its entry in `prefect:concurrency:expirations`) is
created with the slot, and its id travels on the run's states (`lease_id`). The engine renews the
lease of the state each attempt started with: right away, then every 0.75 x its lease duration. A
renewal answered 410 means the slot is no longer reserved, and the engine stops the attempt and
reports the run Crashed rather than run over the limit.

The repossessor scans the expirations index every `REPOSSESS_LOOP_S` and, for each expired lease,
returns its slots and deletes it.

## Operations

- The orchestrator and the repossessor run under the platform's process supervisor with restart
  always; either can be killed at any instant (OOM, deploys, node drain) and is started again.
- When a node is lost its engines vanish without reporting. Their leases run out and the
  repossessor returns the slots. The runs themselves are closed out later by the stale-run sweeper
  or an operator (`admin set-state <run> CRASHED --force`), often well after that.
- `pause_point(...)` calls are hooks for our fault-injection rig and do nothing unless
  `SEAM_SOCKET` is set. Keep them where they are in the flow, with the facts they report.

## Running it

```bash
eval "$(python -m orchestrator.devstack up)"      # RDS PostgreSQL 16 + ElastiCache; exports PG_DSN, VALKEY_URL
python -m orchestrator.server &                   # 127.0.0.1:4200
python -m orchestrator.repossessor &
python -m fleet.worker &

python -m orchestrator.admin deployment ingest --limit 1
python -m orchestrator.admin run ingest --retries 2 --retry-delay 30 --plan 5:fail --then 5:complete
python -m orchestrator.admin runs | slots | leases | history <run>
```

| variable | used by | default | |
|---|---|---|---|
| `PG_DSN`, `VALKEY_URL` | all | | from `devstack up` |
| `ORCH_PORT` / `ORCH_URL` | server / fleet, admin | 4200 / `http://127.0.0.1:4200` | |
| `LEASE_TTL_S` | server | 300 | TTL of a new lease |
| `SLOT_WAIT_S` | server | 30 | delay of AwaitingConcurrencySlot |
| `REPOSSESS_LOOP_S` | repossessor | 15 | scan interval |
| `LEASE_DURATION_S` | engine | 300 | lease_duration sent on renew |
| `WORKER_POLL_S` | worker | 5 | poll interval |
| `FLEET_EVENTS` | fleet | unset | optional JSON-lines event log |

The timings scale down: e.g. `LEASE_TTL_S=8 LEASE_DURATION_S=8 SLOT_WAIT_S=1 REPOSSESS_LOOP_S=1
WORKER_POLL_S=1` runs the same protocol in seconds. Every process takes its settings from the
environment and its connections from `PG_DSN`/`VALKEY_URL`; nothing is shared through local files.
