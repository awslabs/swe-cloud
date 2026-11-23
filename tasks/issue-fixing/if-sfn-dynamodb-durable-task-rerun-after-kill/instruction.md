# Retries start a second load while the first is still running; a console stop went green

The durable task worker in `/app` (`python3 -m durable_task run ...`, see `/app/README.md`) runs
one try of a task instance: it starts the task instance's job (a Standard Step Functions
execution), keeps the task instance's record in the task state store (DynamoDB) and polls the job
until it finishes.

Two incidents last week:

1. A worker host was OOM-killed while a try was starting its job. The scheduler started the next
   try about a second later on another host, and that try started a second execution of the job
   for the same task instance while the first one was still running. Both loaded the same
   partition.
2. Ops stopped a hung load from the Step Functions console. The try that was polling it reported
   success, the task went green, and downstream published a partition that was never loaded.

Please fix the worker. Whatever instant a try is killed at, a task instance must never have two of
its job executions live at the same time, and a later try must still be able to see the job
through. A try whose execution is stopped, fails or times out must fail, and the next try must run
the job again. Once a task instance's job has succeeded, no later try may start it again: a clear
is just another try, and it must come straight back green. A task instance must only ever pick up
executions that belong to it.

What has to keep working:

- The CLI, its arguments and its exit status (0 only when the task instance's job succeeded).
- The task state store record is read by components that are deployed separately and will not
  change with this fix: the reconciler and the ops dashboard (`docs/state-store.md`,
  `docs/operations.md`).
- This ships while tries are in flight: records and executions created by the version deployed
  today must carry on normally under the new code.
- Each try is a fresh process, possibly on another host; nothing on local disk survives to the
  next try. Reach AWS only through the endpoint configured in the environment
  (`AWS_ENDPOINT_URL`).
- Production is AWS. Where the README says Floci behaves differently from AWS, write the code
  for AWS.

Everything can be run locally against the emulator; the README shows how.
