# Operating the durable task worker

## Tries, retries and clears

- The scheduler runs at most one try of a task instance at a time. Each try is a fresh
  `python3 -m durable_task run ...` process on whichever worker host is free, with `--try-number`
  one higher than the last. Nothing on a host's local disk carries over to the next try.
- A try can die at any instant: hosts get OOM-killed and spot capacity is reclaimed (SIGKILL, no
  cleanup runs). When a try ends, the scheduler also kills whatever is left of its process group.
- Exit status 0 marks the task instance successful. Anything else, or a killed process, is a failed
  try; the next try is started about `retry_delay` later (around a second on most dags) while
  tries remain.
- Most failed tries never got as far as the job. A host lost while the try was starting up, or a
  try that failed before it reached AWS, leaves nothing behind in DynamoDB or Step Functions, and
  the next try has to run the job just as the first one would have. Retries are there for exactly
  this.
- Clearing a task instance, in any state, schedules one more try with the next try number. It goes
  through exactly the same code as a retry. Clearing a task whose job already succeeded must bring
  it back green without running the job again.

## Stopping a run from the console

Ops sometimes stop a job execution from the Step Functions console (status `ABORTED`) to unstick a
hung load or to free the warehouse. A stopped execution did not finish its load.

## The reconciler

When a task instance has failed for good — its last try exited non-zero or was killed — the
platform reconciler cleans up after it, so nothing keeps running (and loading) on behalf of a
failed task. It reads the task instance's item from the task state store with a consistent read
and calls `StopExecution` on the item's `execution_arn`, ignoring "does not exist" and "already
stopped". It does not search Step Functions for other executions: what the item names is what it
stops.

The reconciler belongs to the platform team and is deployed separately; it is not in this repo.

## Permissions

In production a try runs as the `durable-task-worker` role, whose only policy is
`infra/worker-policy.json`. Calls outside it are denied.
