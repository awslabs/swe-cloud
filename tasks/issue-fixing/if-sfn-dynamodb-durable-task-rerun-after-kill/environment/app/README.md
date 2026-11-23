# durable_task

Runs one try of an Airflow-style task instance whose work is a long-running job: the warehouse
load, a Standard Step Functions execution of the job's state machine. A try starts the job (or
picks up the one an earlier try started), records it in the task state store (DynamoDB) and polls
it until it finishes.

```
durable_task/
  cli.py        argument parsing, exit status
  resumable.py  what a try does: reconnect, return, or start the job
  job.py        Step Functions calls and status classification
  store.py      the task state store (one DynamoDB item per task instance)
  ti.py         task instance identity
docs/
  state-store.md   the record and who reads it
  operations.md    retries, clears, console stops, the reconciler, permissions
infra/
  bootstrap.py, job-definition.json, worker-policy.json
```

## Running it locally

The emulator (Floci, all services on `http://localhost:4566`) is started by
`bash /usr/local/bin/start_floci.sh`; it is normally already up.

```
python3 /app/infra/bootstrap.py          # table, roles, a local job state machine; prints its ARN
cd /app && python3 -m durable_task run \
    --dag-id etl --task-id load --run-id 'manual__2026-10-01T00:00:00+00:00' --map-index -1 \
    --try-number 1 --max-tries 3 \
    --state-machine-arn "$JOB_ARN" \
    --table task_state_store
```

`JOB_ARN` is the state machine ARN bootstrap prints. The local job just waits 20 seconds and succeeds; stop one with
`awslocal stepfunctions stop-execution --execution-arn ...`.

## The job's input

The job reads `task_instance` from its execution input to know which partition to load:
`{"task_instance": {"dag_id": ..., "task_id": ..., "run_id": ..., "map_index": ...}}`. The ops
dashboard groups executions by the same field.

## How a try is run

- One process per try, started by the scheduler with the CLI above. Tries of one task instance
  never run at the same time, but any try can be killed at any instant, and the next one may run on
  another host: state a later try needs has to be in DynamoDB or Step Functions.
- AWS endpoint, region and credentials come from the environment (`AWS_ENDPOINT_URL` etc.); never
  hard-code an endpoint.
- `run_id` is whatever the trigger supplied. Scheduled runs look like
  `scheduled__2026-08-18T00:00:00+00:00`; manual triggers accept any string.

## Production is AWS, not Floci

Floci is more forgiving than AWS in a few places that matter here. Production behaves like AWS:

- IAM is not enforced by Floci. In production the worker can make only the calls in
  `infra/worker-policy.json`.
- `DescribeExecution` and `ListExecutions` are eventually consistent in AWS: results "may not
  reflect very recent updates", so an execution started moments ago can be reported as not
  existing for a while. Floci answers from its current state.
- Execution names must follow the StartExecution rules: 1–80 characters; no whitespace, no
  brackets ``< > { } [ ]``, no wildcards ``? *``, none of ``" # % \ ^ | ~ ` $ & , ; : /`` and no
  control characters (names made only of `0-9 A-Z a-z - _` also work with CloudWatch Logs).
  Floci may accept names AWS rejects.
