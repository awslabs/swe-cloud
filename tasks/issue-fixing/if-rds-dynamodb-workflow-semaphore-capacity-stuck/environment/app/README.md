# Workflow controller

This service coordinates workflow admission across clusters using database semaphores. The Python port uses RDS PostgreSQL for synchronization and a DynamoDB table for each cluster's workflow API. Execution is external: it watches Running items with recorded synchronization status and a matching SQL hold, then sets exec_done when the work finishes. It continues running admitted work independently of the controller.

Provision a development deployment with `python3 /app/provision.py`. It prints a DSN and a workflow table name. Run a controller with:

```
python3 /app/controller.py --name east --cluster east --workflow-table TABLE \
  --pg-dsn DSN --dynamodb-endpoint "$AWS_ENDPOINT_URL" \
  --heartbeat-seconds 2 --inactive-controller-seconds 15
```

A controller prints `READY` after committing its initial heartbeat and loading running work. Its name is a persistent deployment identity, unique among controllers sharing a database. Relaunches use the same name. Workflow keys are `namespace/name`; they are unique within a cluster, not across clusters. Only the cluster's own workflow table is available at its configured endpoint. All coordination must use the durable services. More than one controller process runs at once.

`schema.sql` is the SQL layout. sync_limit names are `namespace/semaphore`, while sync_state names have a `sem/` prefix. Every held row consumes a slot, regardless of controller. Pending rows are ordered by descending priority and ascending time across controllers with a current heartbeat. A stale controller's pending entries are ignored in ordering, but retained. The primary key includes the controller because clusters can use identical workflow keys.

The workflow table has string partition key `wf_key`. Items contain `uid`, `phase` (Pending, Running, Succeeded, Failed or Error), `semaphore`, integer `priority`, ISO UTC `created_at`, numeric `run_seconds`, and numeric `version` (initially 1). Admission adds `sync_holding`, a list of semaphore names. The executor adds boolean `exec_done`. Every workflow update must compare the identity, version and phase it read and increment version. Readers must paginate and use consistent reads for lifecycle decisions. A failed read is not evidence of deletion. Terminal phases never return to Pending within an incarnation. Clients can delete and recreate a workflow at the same key, starting version at 1 again. Each creation has a unique immutable string `uid`; it is also the SQL row's `execution_id`. A reservation belongs to that incarnation, not to the reusable name.

Normal lifecycle: create Pending item; enqueue and acquire a slot in a serializable SQL transaction; commit it; update the workflow to Running with sync_holding. When the executor sets exec_done, mark Succeeded before releasing its slot. Cancellation may instead fail or delete an item. Keep this protocol and the SQL tables authoritative for admission; replacing SQL with an unrelated lock prevents interoperability. Pauses emitted by the application are part of the deployment interface and must remain in any reimplementation.

The external executor's interface is documented in `executor.md`. Its durable receipts share the cluster workflow table, under reserved `@run/<node_id>` keys. They are not workflow items and must survive workflow deletion and controller restarts. The workflow scan helper excludes this reserved namespace. RDS `sync_state.execution_id` identifies the immutable root node of that incarnation; the SQL primary key continues to enforce one reservation per name and controller. Incarnation IDs are never reused, including across clusters.
