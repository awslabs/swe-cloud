# BwoB builds fail on action-cache hits whose outputs are already gone

**Service:** `cachesvc` in `/app` — our Python port of bazel-remote's disk cache with the S3 proxy
behind it. Two replicas share one bucket. Deployment, S3 behaviour and the rules we hold the
service to are in `/app/docs/OPERATIONS.md`.

Since we enabled `CAS_REFRESH_AGE` and turned the AC dependency check off (the reasoning is in
`/app/docs/cas_refresh_age.md`), builds with `--remote_download_minimal` fail in staging several
times an hour:

```
ERROR: Lost inputs no longer available remotely: bazel-out/k8-fastbuild/bin/core/libcore.a (8d4852a7...)
```

What we know so far: each failing build got an action-cache hit from r2 for an entry that had been
written through r1 shortly before. The output that entry lists had already been deleted from
`cas.v2/` by lifecycle while the entry itself was still in `ac/`. The blobs involved are not
obscure — the one above is a core library output that almost every build on r1 produces and checks
for. Lifecycle follows the README ordering rule (staging: `ac/` expires after 20 s, `cas.v2/`
after 40 s, `CAS_REFRESH_AGE=7`); the storage team owns that configuration and will not change it,
and lifecycle is the only thing that deletes.

Fix `cachesvc` so the bucket never holds an AC entry whose outputs are gone — without giving back
what `CAS_REFRESH_AGE` was for. The latency, S3 cost and storage-reclamation rules in
`OPERATIONS.md` still apply.

Keep the entry point (`PYTHONPATH=/app python3 -m cachesvc`), the environment configuration and
the HTTP API described in `/app/README.md`: staging starts each replica exactly that way, with
its own `LISTEN` and `DISK_DIR` and `S3_ENDPOINT` pointing at the staging S3 front. Don't change
bucket configuration and don't delete objects.

Locally, `bash /usr/local/bin/start_floci.sh` starts an S3 emulator on `http://localhost:4566`
(no lifecycle, no added latency, no throttling); `cd /app && python3 -m pytest -q tests` runs the
unit tests.
