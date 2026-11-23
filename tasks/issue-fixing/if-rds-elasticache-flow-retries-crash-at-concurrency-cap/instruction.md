# Flows with retries crash on concurrency-limited deployments

`/app` is our flow orchestration server and, under `/app/fleet`, the client release our workers run
(see `/app/README.md`). It keeps runs and deployment concurrency counters in RDS PostgreSQL and the
concurrency leases in ElastiCache.

Since we put a concurrency limit of 1 on the ingest deployments, flows that have `retries` no longer
retry there. A failure that should go to `AwaitingRetry` and run again instead ends `Crashed` with
`run_count=1`, and the engine log says:

```
Concurrency lease renewal failed - slots are no longer reserved. Terminating execution to prevent over-allocation.
```

Short retry delays sometimes get through; long ones die every time. The same flows retry fine on
deployments without a limit.

Possibly related, possibly not: twice last month, after we lost a worker node, on-call saw two runs
of a limit-1 deployment executing at the same time for a while. Nobody pinned it down.

What we need:

- Retries on limited deployments behave as they do anywhere else.
- A deployment's limit is never exceeded, whatever happens to the processes involved: supervisors
  drained with SIGTERM, nodes lost, and the orchestrator or the repossessor killed and restarted at
  any moment (see Operations in the README).
- Slots do not leak: once a deployment's runs have all finished, its `active_slots` is 0 and none
  of its leases are left in Valkey; a slot that has been given back stays given back.

The fix belongs in the server, `/app/orchestrator`. The fleet in `/app/fleet` is deployed as it is
and will be run unchanged against your server, so do not rely on changing it. Keep the HTTP API,
the environment settings, the shared tables and the Valkey lease layout as they are, and keep the
`pause_point` calls where they are in the flow. You may add tables of your own.

Floci serves the AWS APIs at `AWS_ENDPOINT_URL`. `python -m orchestrator.devstack up` creates an RDS
instance and an ElastiCache cluster for you, and the README shows how to run the whole system.
