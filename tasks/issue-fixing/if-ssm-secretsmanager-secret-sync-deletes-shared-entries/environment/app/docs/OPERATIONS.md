# Operating syncctl

## Where it runs

Each sync is one Kubernetes CronJob in the `platform-sync` namespace. A run is one pod and one
`syncctl` process. The jobs run on spot capacity.

- **Every run is a fresh pod.** The working directory, `HOME` and `TMPDIR` are new each time, and
  nothing written to local disk survives into the next run. A sync's only durable state is in AWS.
- **A run can die at any instant.** Spot reclaims, OOM kills and `kubectl delete pod
  --grace-period=0` end the process with SIGKILL. That can land between any two AWS calls, with
  no signal handler, `finally` block or retry running. Nobody repairs a half-finished run by
  hand. The next scheduled run of that sync, a `sync` or the final `remove`, is the recovery,
  and it has to bring the destination back to what README "Ownership" and "Commands" promise.
- **Replies get lost.** The jobs reach AWS through the cluster's NAT gateway, which now and then
  resets a connection while a call is in flight. A call AWS has already carried out can lose its
  reply that way, and the SDK's retry policy (`syncctl/aws.py`) then sends the same call again.
  A lost reply is not a failed run: the run works out from AWS what the call did and finishes
  its job, and exits 1 only when it really cannot.
- Runs of one sync never overlap (`concurrencyPolicy: Forbid`). Different syncs run at different
  times.

## Shared destinations

| destination | who writes there |
|---|---|
| Parameter Store `/` | `payments-root` and `checkout-root` syncs; ops' hand-managed parameters (pager tokens, legacy URLs, vendor keys), created with the console or `aws ssm put-parameter`, some tagged `owner=<team>`, many with no tags at all |
| Secrets Manager (account-wide) | `app-*` and `reports` syncs, plus secrets other teams create by hand (`app/...`, `db/...`, legacy `INFISICAL_*` names from the previous tool) |

Hand-managed entries are not yours just because they look like something you would have
written; ops sometimes create a parameter by hand before the sync that will want it is onboarded.

When a key leaves a sync (dropped from its source, or the whole sync removed) and some team still
needs it, that team takes it over by hand: a small script waits for the synced entry to disappear
and creates the team's own entry under the same name, usually with the same value, the moment the
name is free, so consumers are without it as briefly as possible.

People also delete secrets by hand in the Secrets Manager console: teams retiring their own
secrets, and on-call now and then on a synced secret by mistake. The console always schedules the
deletion with a recovery window (7 to 30 days); it never deletes at once.

None of these people coordinate with the CronJobs: hand-made entries appear, and console deletes
land, at any time, including in the middle of a run.

## Running it locally

The dev container has an AWS emulator on `http://localhost:4566`. If it is not up, run
`bash /usr/local/bin/start_floci.sh`. Then:

```
export AWS_ENDPOINT_URL=http://localhost:4566 AWS_DEFAULT_REGION=us-east-1 \
       AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test
/app/bin/syncctl sync --config /app/examples/example-ssm.json
aws ssm get-parameters-by-path --path /example/ --with-decryption
/app/bin/syncctl remove --config /app/examples/example-ssm.json
```

Emulator quirk: a *recursive* `GetParametersByPath` on `/` also returns AWS's public parameters
under `/aws/service/...`. They are not resources of the account: `ListTagsForResource` on them
answers `InvalidResourceId`. Production accounts don't list them under `/`.
