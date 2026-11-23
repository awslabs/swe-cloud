# cadence

A small highly-available job scheduler. Two scheduler processes contend for a
lease in DynamoDB; the owner decides when each configured job is due and admits
an attempt; worker processes take admitted attempts and run their bodies. All
state that has to survive a process - the lease, the jobs, the decisions, the
running records - is in one DynamoDB table. Nothing of value is in process
memory or on local disk: the processes are interchangeable and stateless.

Jobs are configured `forbid`: while one attempt of a job owns it, no other
attempt of that job may be started, by whoever is deciding at the time.

## Processes

```
scheduler --id ID --state-table NAME [--endpoint URL] --control-fd FD
worker   --id ID --state-table NAME [--endpoint URL] --control-fd FD --callback-socket PATH
python3 -m proto init   [--state-table NAME]           # create the state table
python3 -m proto dump   [--state-table NAME] --pk PK   # print one partition
```

Both processes write one JSON line to their control file descriptor when they
are ready: `{"type": "READY", "role": ..., "id": ...}`. The descriptor is one
end of a socket pair; JSON lines travel both ways on it. The endpoint defaults
to `AWS_ENDPOINT_URL` (the emulator at `http://localhost:4566`).

### Control pipe

The launcher may write JSON lines to the same descriptor:

- `{"type": "STOP"}` - shut down cleanly.
- `{"type": "COMPLETE", "job": ..., "run_id": ..., "claim_token": ..., "result": ...}`
  - apply a completion through the same path a body's own return uses. The
    worker answers on the same descriptor with
    `{"type": "COMPLETE_RESULT", "job": ..., "run_id": ..., "accepted": ...}`.

### Body callback

A worker never does work silently. Before the body it opens the callback socket
and sends one JSON line:

```
{"type": "START", "job": ..., "slot": ..., "run_id": ..., "claim_token": ..., "pid": ...}
```

It then waits for `{"granted": true}` - however long that takes; the holder of
the callback decides when work may begin - runs the body, and sends
`{"type": "END"}` on the same connection when the body returns. A worker must
keep its claim maintained while it waits, and must not report a completion for
a body that has not returned.

## Item layout

| key | contents |
|---|---|
| `META` / `JOB#<job>` | `{job, period_s, targets, start_at, concurrency}` - the job definition and the workers that may run it |
| `META` / `ARM` | `{anchor, slots}` - written once by the launcher; periodic nominals exist only after it |
| `CLUSTER` / `LEADER` | `{owner, epoch, lease_until}` - the current schedule owner |
| `JOB#<job>` / `ACTIVE` | the running record: `{run_id, claim_token, slot, status, worker?, targets, lease_until, heartbeat_at, started_at}` |
| `JOB#<job>` / `SLOT#<nnnn>` | `{slot, nominal_at, disposition, decided_at, run_id?, claim_token?}` - one per nominal, written once |
| `JOB#<job>` / `RUN#<run_id>` | `{job, slot, run_id, claim_token, worker?, status, started_at, finished_at?, result?}` - the attempt's record |
| `JOB#<job>` / `CURSOR` | `{next_slot}` - how far the job has been decided |

`ACTIVE` is the running record of the job: which attempt owns it right now.
`status` is `LIVE` while the attempt holds the job. `claim_token` identifies the
attempt's credentials; `worker` is unset until a worker has taken the claim, and
only the first taker can set it. Minting, taking and every maintenance write
set `heartbeat_at` to the time of the write and `lease_until` to `heartbeat_at +
run_lease_s`.

## Decisions

Nominals are numbered per job: slot 0 is due at the job's `start_at`, slot k at
`anchor + k * period_s`. Each nominal gets exactly one terminal decision, in
order, recorded in its `SLOT#` row:

- `RUN` - admitted; an attempt is minted and a named worker takes it;
- `SKIPPED_BUSY` - a running attempt already owned the job;
- `MISSED_OUTAGE` - no decision landed within `decision_window_s` of the
  nominal; that nominal is lost and is never replayed.

The cursor only moves forward. A scheduler that does not own the lease decides
nothing. The lease is taken only when nobody holds a live one; each takeover
advances `epoch` by one and never rewinds it.

## Tuning

`poll_s` 0.1, `leader_lease_s` 3, `run_lease_s` 6, `heartbeat_s` 1,
`decision_window_s` 2, `short_body_s` 0.5 (see `proto.py::CONFIG`).

## Running it by hand

The quickest way to see it work:

```
python3 tools/standalone.py --job demo --slots 4
```

That creates a private table, installs one job, starts the body callback, one
scheduler and one worker, and tails their logs (`/tmp/standalone_s1.log`,
`/tmp/standalone_w1.log`). `--arm-after S` waits S seconds before writing the
anchor, if you want the periodic nominals to arrive after the processes are
already running.

By hand, piece by piece:

```
python3 -m proto init                                    # state table
python3 - <<'EOF'
import sys; sys.path.insert(0, "/app")
from proto import State
st = State("cadence_state")
st.put("META", "JOB#demo", {"job": "demo", "period_s": 4, "start_at": __import__("time").time() + 2,
                            "concurrency": "forbid", "targets": ["w1"]})
EOF
python3 tools/callback.py /tmp/body.sock &               # acknowledges bodies at once
python3 scheduler.py --id s1 --state-table cadence_state --control-fd 3 3<&0 &
python3 worker.py   --id w1 --state-table cadence_state --control-fd 4 \
                    --callback-socket /tmp/body.sock 4<&0 &
python3 -m proto dump --pk 'JOB#demo'
```

With no `META/ARM`, only slot 0 is due; write `{"anchor": now, "slots": 4}` to
`META/ARM` to arm the periodic nominals. The processes are meant to be killed
with SIGKILL and restarted with the same id; the table is where everything of
consequence lives.
