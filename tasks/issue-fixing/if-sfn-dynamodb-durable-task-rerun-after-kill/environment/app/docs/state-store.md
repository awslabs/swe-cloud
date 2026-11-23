# Task state store

One DynamoDB item per task instance, in the table passed as `--table` (`task_state_store` in
every environment). The item is shared by every try and every clear of that task instance; other dag
runs, and other map indexes of the same task, have items of their own. Items are not deleted when a
task finishes.

| attribute | type | meaning | read by |
|---|---|---|---|
| `ti_key` | S | partition key: `json.dumps([dag_id, task_id, run_id, map_index], separators=(",", ":"))` | everyone |
| `execution_arn` | S | the job execution that belongs to this task instance: the one it is running, or the one it ran last | the worker on later tries, the reconciler, the ops dashboard |
| `try_number` | N | the try that last wrote the item | humans |
| `updated_at` | S | ISO-8601 time of the last write | humans |

The version of the worker deployed today writes exactly these four attributes. Readers ignore
attributes they do not know, so the worker may add its own.

Reads that make decisions use `ConsistentRead`.
