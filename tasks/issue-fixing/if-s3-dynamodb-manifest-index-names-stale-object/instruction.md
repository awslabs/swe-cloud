# Readers refuse freshly committed versions; a writer was told "conflict" for a version it won

`/app` is strata, our dataset-manifest store: ingest jobs commit versions with
`stratactl commit`, readers open them with `stratactl resolve`. The protocol is described in
`/app/docs/PROTOCOL.md`; who runs what, and the environment it runs in, is in
`/app/docs/OPERATIONS.md`. Floci (S3 + DynamoDB) is running at `http://localhost:4566`;
`python3 /app/scripts/bootstrap.py` creates the table and bucket.

## What we're seeing

1. Over the last two weeks dashboards have intermittently failed to open a version right after it
   was committed: `stratactl resolve` exits 2 with `Manifest generation mismatch for version N`.
   Most clear up on their own within a second or two. `warehouse/orders` v41 never did: every
   reader, on every release, still refuses it, although its manifest bytes are intact. What we
   captured is in `/app/ops/orders-v41.txt`.
2. `nightly-merge` was told `conflict` for `warehouse/events` v12, re-planned, and committed the
   same batch again as v13. v12 was its manifest all along. See `/app/ops/events-v12.txt`.
   `stream-ingest` landed a batch twice the same way on `warehouse/sessions` v7
   (`/app/ops/sessions-v7.txt`).

## What we need

- A version whose manifest is intact opens with this release's `resolve`, including versions
  already in the state v41 is in.
- Nothing this release writes to the index may get a version refused while its bytes are fine,
  at any moment — by this release's reader or by the 0.14 reader the batch fleet keeps running
  (the one in `/app` today).
- A canonical manifest that is not the manifest the index selected must still be refused
  (exit 2).
- `commit` says `committed` exactly when its manifest became the version, and `conflict` exactly
  when another writer's did.
- `generation` in the output stays what the query service's cache needs: the generation of the
  canonical object this process observed.
- Committers stall and die mid-commit. Readers have to keep finishing pending versions on their
  own, within the latency budget in OPERATIONS.md.

Keep the CLI, its output and exit codes, and the table and bucket layout. Every AWS call must go
to `AWS_ENDPOINT_URL`, and each command runs as its own process in its own working directory, so
nothing kept on local disk is shared between them. Don't rely on request headers the gateway
rejects (see OPERATIONS.md).
