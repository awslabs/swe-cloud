# chambersm

`chambersm` is our Python port of the Secrets Manager backend of
[chamber](https://github.com/segmentio/chamber). It stores the configuration secrets of each
service and is how deploy pipelines set them (`ci/set-secret.sh`).

```
python3 -m chambersm write   <service> <key> <value>
python3 -m chambersm read    <service> <key> [--version N]
python3 -m chambersm history <service> <key>
python3 -m chambersm delete  <service> <key>
python3 -m chambersm list    <service> [--values]
python3 -m chambersm export  <service>
```

Run from `/app` (or with `PYTHONPATH=/app`). `write` exits 0 once the value is stored; that exit
status is what the pipeline treats as "secret set". `history` prints one JSON object per line,
oldest first: `{"event", "version", "value", "created", "created_by"}`.

## Storage

One secret per service, named after the service. Its `SecretString` is a JSON object:

```json
{
  "db_password": "s3cr3t",
  "api_token": "abc123",
  "_chamber_metadata": "{\"db_password\":{\"created\":\"2026-10-01T09:57:47.1Z\",\"created_by\":\"deploy/8812\",\"version\":2},\"api_token\":{...}}"
}
```

- Top-level fields are exactly the service's keys plus `_chamber_metadata`. Services consume the
  secret directly (ECS `valueFrom` with a JSON key, `export`), so anything else at the top level
  becomes configuration.
- `_chamber_metadata` is a *string* holding JSON: per key, when it was written, by whom
  (`CHAMBER_WRITER_ID`, set by the CI wrapper), and its per-key version number. Version 1 is the
  first write of a key; each write of the key increments it.
- `history` and `read --version` replay the versions of the secret that Secrets Manager lists
  (`ListSecretVersionIds` without deprecated versions) and pick each key's entries out of their
  metadata. A version with no staging label is deprecated: it is not listed, and AWS deletes
  deprecated versions once a secret has more than 100 versions. Any value that should stay in
  history therefore has to be on a version that keeps a label.
- Secrets Manager allows at most **20 staging labels attached across all versions of a secret**,
  AWSCURRENT and AWSPREVIOUS included. The quota is not adjustable. A `PutSecretValue` or
  `UpdateSecretVersionStage` that would attach a 21st label fails with
  `LimitExceededException` and changes nothing.

## Operating notes

- **Who writes.** Deploy pipelines, the credential-rotation pipeline and people running
  `ci/set-secret.sh` by hand. Each `write` is its own job on its own ephemeral runner; jobs share
  no disk, no host and no lock service, and several of them regularly write to the same service
  at the same moment.
- **Timeouts.** `ci/set-secret.sh` runs the write under `timeout --signal=KILL 20`. A job that
  has not finished within 20 s is killed outright, at whatever point it has reached.
- **Runners stall.** Runners are preemptible VMs. We regularly see a job freeze in the middle of
  a step (host suspended, CPU starved) and carry on a few seconds later as if nothing happened.
  While it is frozen, other jobs keep writing.
- **Dropped connections.** The Secrets Manager VPC endpoint now and then drops a connection
  after Secrets Manager has received a request and applied it, so the job gets a connection
  error instead of the answer. The SDK retries the request with its default retry settings. A
  dropped connection is no reason for a write to fail, and a write is still one write.
- **Permissions.** Writers assume the service's `chambersm-writer` role,
  `iam/chambersm-writer.json`: the six Secrets Manager actions listed there, on that service's
  own secret, and nothing else. Any other call is `AccessDeniedException`.
- **History retention.** On-call needs `history` to reach back at least 15 writes: every write
  to a service must stay in `history` (and readable with `read --version`) until at least 15
  further writes to that service have happened. Older versions may drop out. This covers the
  history our services already have: the fix must not cut short what they show today.
- **Endpoint and credentials** come from the standard AWS SDK environment (`AWS_ENDPOINT_URL`,
  `AWS_REGION`, ...). CI points them at the Secrets Manager VPC endpoint; locally they point at
  Floci. Do not hard-code endpoints.

## Local development

A Floci emulator is running on `http://localhost:4566` (`AWS_ENDPOINT_URL` is already set; region
`us-east-1`, credentials `test`/`test`). Floci does not enforce IAM, and it does not enforce the
Secrets Manager label quota either; AWS does. If it is not running,
`bash /usr/local/bin/start_floci.sh` starts it.

```
cd /app
python3 -m chambersm write demo db_password hunter2
python3 -m chambersm history demo db_password
python3 -m pytest -q tests
```

`tests/` holds unit tests against an in-memory fake of the Secrets Manager client.
