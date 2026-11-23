# Delegated batch runner

`python -m harness.service` provisions a uniquely named RDS PostgreSQL 16.3 instance
through Floci, creates a run, and runs extract → (load, audit) → publish. It prints the resource name,
connection details and run id. `floci-venue up` is the environment boot command.
`python -m harness.service --help` describes the normal launcher. AWS uses
AWS_ENDPOINT_URL and the usual local test credentials. No production account is used.

The controller runs each Job's argv as a pod process, using a separate working
directory and PostgreSQL login per pod. Each gets PGHOST/PGPORT/PGDATABASE,
PGUSER/PGPASSWORD, POD_NAME, DAGSTER_RUN_ID, DAGSTER_STEP_KEY and
DAGSTER_STEP_ATTEMPT. Replacement pods inherit the same Job argv and attempt.
The process supervisor in `harness.supervisor` restarts run workers independently
of step Jobs; rolling updates may leave two run workers for the same run.
The run worker starts as `python -m solution.run_worker --run-id ID` with a
run_worker database login. It must mark the run STARTED and submit the first Job
within ten seconds of launch on a new run. Restarting the run worker resumes
the same run and its existing Jobs; terminal runs stay terminal. Submit steps only through INSERTs into dagster.k8s_jobs;
the controller owns pod creation and pod status. Job names include the run id so
multiple runs can coexist. Cold extract images take seven seconds to initialize before the worker entrypoint
(`harness.pod`); load and retry images are warm. Preserve that entry adapter and
its `pod_boot` announcement. There is room for four simultaneous pods per run and
one replacement per Job. See `harness/schema.sql` for grants and data shapes.

`solution.storage` rebuilds attempt state from ordered events. STEP_START (or
STEP_RESTARTED) opens an attempt; STEP_UP_FOR_RETRY closes it without completing
the step; STEP_SUCCESS / STEP_FAILURE close it with an outcome. The business graph, batch counts and retry budgets are in `harness/workflow.py`.
Extract permits one application retry; the other steps have none. Load and audit
are independent branches: submit both when extract succeeds, without waiting for
either branch to finish. Publish consumes both successful branches. A Job replacement is not an application retry.
The ledger and events are append-only and draw IDs from one sequence. The database
sets the writer identity from session_user. Do not replace those tables or triggers.
Auditors compare that identity: an attempt's STEP_SUCCESS must be written by the same pod login
that wrote its batches; run workers may close attempts for retry, failure or cancellation but never
publish success on a pod's behalf.
You may create and change coordination tables in schema solver. Tables created by
run_worker there are accessible to the pod group through default privileges.

The body is provided by `harness.step_body.run(step_key, attempt, ctx)`. It produces
the batch counts in `harness/workflow.py`, with one second between batches. Extract's
batch 4 and audit's batch 1 run nine-second database transformations inside
their transactions.
The ledger's deferred validator checks the payload and models four seconds of
commit-time work for extract batch 1 and audit batch 0 (see `harness/schema.sql`).
Call the body exactly once per admitted worker. Supply
`ctx.commit_batch(batch_no, write)`; `write(conn)` performs that batch's SQL on an
open psycopg transaction. `commit_batch` must return only when the batch is durable,
and raise on revocation so the body stops quietly. The batch number is stable for
replays within an attempt; the next attempt has its own complete batch set.

The orchestration owns connection retry and transaction completion. A connection
error can happen before, during or after PostgreSQL commits: a missing acknowledgement
does not establish whether the batch landed or whether its original transaction
has finished yet. Other connections can continue making progress while it resolves.
A batch must become durable exactly once within its admitted attempt, including
when `write` needs replay. The ledger has no uniqueness constraint; other historical imports use the same table.
Do not change its schema or put orchestration into the supplied body. Preserve
`body_enter` and `batch_insert` announcements; they are no-ops without a driver.
The local controller routes pod SQL through `harness.transport`, a transparent
PostgreSQL connection relay which can model a lost connection at transaction
boundaries. It neither retries SQL nor decides worker authority. Run workers and
pods use the supplied PGHOST/PGPORT; do not bypass their configured endpoint.

`dagster.run_inputs` is immutable business input. The launcher accepts
`--reject-load` to model a validation error after load's first committed batch.
Application exceptions are attempt failures; they are not connection retries.
STEP_CANCELLED closes a submitted peer attempt abandoned because another branch
failed. It is not success and does not consume an application retry. A peer Job
may be cancelled before STEP_START; never fabricate a start or invoke its body.
No Job or synthetic event is required for a dependency that was never submitted.

Read `OPERATIONS.md` before changing failure handling.
