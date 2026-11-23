# lake maintenance

- `orphan_clean.py` — remove_orphan_files for one table (scheduled nightly per table).
- `lakectl.py` — operator tool: create a demo table, commit, expire, show.
- `lake/` — the table-format library shared with the ingestion jobs.
- `TABLE_FORMAT.md` — layout, commit and expiry semantics.
- `ops/` — scheduler output from recent runs.

## Running locally

S3 (Floci) answers on `http://localhost:4566` with the local `test`/`test` credentials.

```bash
python3 /app/lakectl.py demo --table-prefix tables/demo/          # prints cutoff_ms
sleep 2
python3 /app/orphan_clean.py --endpoint-url http://localhost:4566 --bucket paimon-lake \
    --table-prefix tables/demo/ --older-than-ms <cutoff_ms>
python3 /app/lakectl.py show --table-prefix tables/demo/
```

## How the scheduler runs the cleaner

One process per table, started in a fresh working directory with `PYTHONPATH=/app`, pointed at
the storage gateway with `--endpoint-url` (the gateway forwards to S3; it is also exported as
`AWS_ENDPOINT_URL`). The cleaner's role can list, read, stat and delete; writes are refused.
The cutoff is passed as `--older-than-ms`. A run that is still going after 60 seconds is killed.
The ingestion jobs keep committing and expiring while the cleaner runs; nothing pauses them.
