# Incident brief: account custom-field sync destroys and regresses fields during provisioning

You own `aft-sync`, the CLI that stands in for the account-metadata step of the account
provisioning pipeline. It synchronises a member account's custom fields — the key/value map
carried on the account request record — into SSM Parameter Store, one String parameter per field
under `/aft/account-request/custom-fields/`, in the account-factory's local AWS endpoint.

## What was seen to go wrong

Two nights ago an account customization ran while a provisioning sync was in flight for the same
account. The customization sizes a pool of member-account resources from one of the custom
fields; during the sync it could not find the field, treated the pool as empty, and tore down
every resource it managed. The field was back a few seconds later and the end state in Parameter
Store looked completely normal, so nothing in the console explains the outage afterwards.

The sync's own log from that window, for a record that had not removed any field:

```
Deleting SSM params: ['/aft/account-request/custom-fields/backup_tier', '/aft/account-request/custom-fields/cost_center', ...]
Adding/Updating SSM params: {'backup_tier': 'gold', 'cost_center': 'cc-123', ...}
```

A single-operator repro earlier today: start the sync for a record change and read the path
concurrently — every field under the path disappears and comes back, even fields the record
kept. The incident is reproducible, not cosmic.

## What must hold afterwards

1. A field the account request **keeps** across a change is never observed absent from the path
   by a concurrent reader, and its value never regresses once the record's field set has been
   applied — including when the change that removed it is followed by a change that re-adds it.
2. A field **removed** from the account request is gone from Parameter Store once a sync for a
   later version of the record has finished. This is the platform's documented behaviour: a
   custom field removed from the account request is removed from the SSM Parameter Store.
3. Once syncing settles, the parameters under `/aft/account-request/custom-fields/` equal the
   current account request's custom fields exactly — no extra names, no missing names.

Consumers read the path live with `GetParametersByPath` on the latest values; they use no
parameter labels or pinned versions, and a customization reading a field that is mid-flight
missing acts on its absence immediately. The per-field layout is the consumer contract: one
parameter per field under the prefix, named `<prefix><field-key>`.

## The operating conditions you must hold them under

These are properties of the pipeline as deployed, not hypotheses:

- One sync invocation is started for each committed change to the account request. Invocations
  are **not ordered** and are **not queued**: two of them may run at the same time, and one
  carrying an older snapshot of the record may run after — or interleaved with — one carrying a
  newer snapshot.
- The payload given to an invocation is a **snapshot of the account request item at the commit
  that triggered it**. It may be older than the item's current state by the time the invocation
  runs.
- The account request item carries a monotonically increasing `record_version`, and the payload
  carries the version it was snapshot from.
- Calls through the account-factory's endpoint may stall: a single request can sit for many
  seconds before the service processes it — a little over twenty at worst, always well short
  of the 30-second limit below. The request is not lost — it is applied when it goes through,
  whether or not the caller is still waiting.
- An invocation may be killed at any point in its work; its payload is retried immediately as a
  fresh invocation, and the retry is expected to complete within about 20 seconds of the kill.
  The endpoint drops whatever requests a killed invocation still had in flight, so nothing of
  its work lands after the kill.
- Every invocation must finish, with exit status 0, within 30 seconds.
- Exit 0 means: the parameters under the prefix equal the custom fields of a record version at
  least as new as the payload's — or a newer version has already been applied and this
  invocation made no change.
- Invocations run under distinct local service accounts with private home and temp directories
  and a read-only `/app`, and no invocation outlives its process group. There is no shared local
  filesystem state between invocations: anything that must survive a process, or be seen by the
  next invocation, belongs in AWS. Two DynamoDB tables exist for this: `aft-request` (hash key
  `id`, the account request items, attributes `custom_fields` — a JSON-encoded map — and
  `record_version`) and `aft-sync-state` (hash key `pk`), pre-created for the sync's own
  bookkeeping.

## What must be preserved

- The CLI contract: `aft-sync --payload <file.json>`, reading its AWS endpoint from
  `AWS_ENDPOINT_URL` (region `us-east-1`), exit status as specified above. The payload file
  format is documented by the tool itself (`aft-sync --help`); an example payload is in
  `/app/payload-example.json`.
- The parameter layout and the consumer contract above.
- Deletions of removed fields and creation/update of kept fields both remain part of the job.

## How to run it

The local AWS endpoint is up at `http://localhost:4566` (region `us-east-1`, credentials
`test`/`test`). Inspect and exercise the tool against it freely — for example, put an item in
`aft-request`, write yourself a payload mirroring it, and run:

```
aft-sync --payload /tmp/my-payload.json
aws --endpoint-url http://localhost:4566 ssm get-parameters-by-path --path /aft/account-request/custom-fields/ --recursive
```

There is no test suite, no canary and no dashboard for this service; the incident brief above,
the tool, and the endpoint are what you have. Restore the sync so that both halves of its job
hold under the conditions above.
