# Purchase operations

This service records an approval in DynamoDB, calls the account purchase gateway,
and publishes the execution outcome for Finance. `worker.py` is also the scheduler
entry point. The gateway is a separate service; it is the only supported interface
to provider commitments. See gateway-api.md for the three product adapters.

The deployed approval request has a five-second execution deadline. A worker can
be destroyed at any instruction, including while waiting for the gateway. The
scheduler likewise has a five-second deadline, can overlap another invocation,
and can be restarted independently. No worker survives its invocation. Each
worker has a fresh working directory; use AWS services for all shared durable state.

Runtime configuration:

| setting | default | deployment meaning |
| --- | --- | --- |
| AWS_ENDPOINT_URL | http://localhost:4566 | DynamoDB endpoint |
| AWS_DEFAULT_REGION | us-east-1 | region |
| EXECUTIONS_TABLE | executions | execution ledger |
| PROVIDER_URL | http://127.0.0.1:8080 | account gateway |
| STALE_APPROVED_THRESHOLD_S | 30 | minimum approved age before maintenance may change it |
| CLAIM_LEASE_S | 30 | lifetime of a maintenance worker's ownership |

The age margin is deliberate: a slow approval must finish on its own, without a
scheduler changing its row or buying alongside it. Maintenance ownership must not
be taken from an unexpired worker. An interrupted maintenance invocation is not
an operator workflow: later invocations still need to settle its orders.

## Ledger

The table key is `execution_id` (string). `updated_at` and `completed_at` are epoch
milliseconds. `recommendations` is a list of maps: `index` (stable integer identity,
not necessarily its list offset), `provider_type`, `offering_id`, `count`, and
`selected`. Purchases record `commitment_id` and `purchased`. Unselected entries
must remain unpurchased. Entries can target different products in one order.
Maintenance reconciles the existing execution; it must not create replacement rows.

`pending` and `notified` may be approved once; all other approval attempts fail.
`approved` means approval was accepted. `running` is maintenance-owned work.
`completed` means every selected entry has a real commitment reference.
`partially_completed` means some selected entries succeeded and others did not;
keep successful references so a failed sibling cannot cause those purchases again.
`failed` means none could be resolved. An unresolved outcome needs a useful error
for the operator, rather than an invented success. Terminal history is durable:
maintenance must preserve completed/partial/failed rows and approvals must not
resurrect them. It must not buy while a live invocation owns the order.

## Operator Retry

Finance uses `python /app/worker.py retry --execution-id ID` after reviewing a
`failed` or `partially_completed` execution. It returns a pending execution ID;
the ordinary approve command is still required to authorize its execution. A
retry is a continuation of the same purchase intent, with the same selected
recommendations, quantities and stable indexes. It is not authorization for
additional commitments. Successful references and evidence about unresolved
purchases must survive through any number of retry generations. An ambiguous
keyless purchase still needs account verification; clicking Retry is not proof
of absence. A recoverable native-token entry should reconcile automatically. Older
terminal rows can predate purchase checkpoints: a failed status by itself is not evidence
that the provider created no commitment.

The API is used by a UI that resends requests when it loses a reply. Concurrent
or repeated Retry calls for one parent must return the same child ID, including
after process loss; never expose a child without recording its acceptance. Only
failed/partial parents are eligible. Retrying an active, pending or completed
execution is an error. Retrying the returned child later is a separate generation,
allowed only after that child has itself finished unsuccessfully. Parent purchase
history remains intact; recording retry acceptance metadata must not change its
purchase outcome. The child records `retry_of` for audit. Its successful
siblings must remain represented, and genuinely unattempted siblings can still
be bought. This API and the scheduler can be invoked concurrently.

## Local operation

The endpoint is started by `/usr/local/bin/start_floci.sh`. The development gateway
runs without injected faults and is useful for ordinary calls:

```
python /app/dev_gateway.py --port 8080
```

In another shell, initialize the ledger with `python /app/worker.py init`.
To submit an order, put a list such as
`[{"provider_type":"token","offering_id":"hourly-compute","count":1}]`
in a JSON file and run `python /app/worker.py submit --file PATH`.
Use its returned ID with `approve --execution-id ID --actor buyer@example.org`.
`python /app/worker.py sweep --once` is one scheduler invocation. Inspect history
with the DynamoDB API. The development gateway's account tables are its private
storage; editing them is equivalent to editing a provider's billing system.
