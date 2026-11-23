# Operating strata

## Who runs what

| fleet | runs | release |
|---|---|---|
| ingest jobs (`nightly-merge`, `stream-ingest`, `orders-loader`, compactors) | `stratactl commit` | current |
| query service | `stratactl resolve` | current |
| batch fleet (reporting, exports) | `stratactl resolve` | pinned to **0.14.x**, the reader in this repository today, until the Q1 platform upgrade |

The batch fleet cannot be upgraded in lockstep with the rest, and its readers keep finishing
pending versions with the 0.14 code. Anything the current release writes to the index has to stay
acceptable to the 0.14 reader at all times.

## How callers use the output

- `stratactl commit` prints `{"status": "committed" | "conflict", ...}` and exits 0 or 3. On a
  conflict an ingest job re-plans its batch against the newest version and commits it again at the
  next version number. A conflict reported for a version the job actually won therefore lands the
  same batch twice.
- Ingest jobs run under a scheduler that reruns a step whose process died (preempted,
  OOM-killed) with the same command line: same dataset, version, manifest file and `--committer`.
  `--committer` names the job, not the attempt; a job's reruns and the replicas of a compactor
  all commit under one name.
- `stratactl resolve` prints `{"version", "path", "size", "generation"}`. The query service caches
  opened datasets keyed by `(base_uri, version, generation)`. `generation` must be the generation
  of the canonical object as this process observed it; a missing or wrong value lets a cached
  dataset from a previous incarnation of the table (dropped and recreated at the same URI) be
  served for the new one.
- Exit 2 from `resolve` means the canonical manifest is not what the index selected; callers stop
  and page. Exit 4 means the version does not exist.

## The environment it runs in

- Every AWS call goes to the gateway named by `AWS_ENDPOINT_URL`. Each process gets its own
  gateway endpoint; nothing on local disk is shared between processes, and every command runs in
  its own working directory.
- The gateway sometimes stalls an individual request for tens of seconds before forwarding it, and
  occasionally answers 500. Clients retry with botocore's standard mode (3 attempts).
- The gateway rejects conditional creates and copy preconditions (`If-None-Match`,
  `x-amz-copy-source-if-*`) with 501.
- Committers are preempted and OOM-killed, including in the middle of a commit. Readers finish a
  pending version themselves; nobody comes back to finish it for them.
- Latency budget: `resolve` and `commit` each return within 20 s of the gateway answering their
  own requests. For `resolve` that includes finishing a pending version: it never waits for a
  stalled committer.

## Running it locally

```bash
python3 /app/scripts/bootstrap.py                       # table + versioned bucket on AWS_ENDPOINT_URL
python3 /app/stratactl commit --dataset warehouse/orders --version 1 \
    --manifest-file manifest.json --committer me
python3 /app/stratactl resolve --dataset warehouse/orders --version 1
python3 /app/stratactl resolve --dataset warehouse/orders --latest
aws dynamodb get-item --table-name strata-manifests --consistent-read \
    --key '{"base_uri":{"S":"s3://strata-warehouse/warehouse/orders"},"version":{"N":"1"}}'
aws s3api list-object-versions --bucket strata-warehouse --prefix warehouse/orders/_versions/
```

`STRATA_LOG_LEVEL=INFO` turns on more logging (stderr).
