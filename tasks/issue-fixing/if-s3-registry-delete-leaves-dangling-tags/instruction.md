# Tags listed by the registry that can't be pulled or deleted

Since Tuesday night's janitor run, about 300 tags across `ci/builder`, `ci/runner` and a few
service repositories show up in `GET /v2/<repo>/tags/list` but answer `404 MANIFEST_UNKNOWN` when
anyone pulls them, and the nightly audit flags every one of them as broken. The janitor can't clean
them up: deleting those digests again now answers `404`, which it takes to mean "already gone".

The run overlapped a rolling deploy of `registry-a` (replicas are SIGKILLed five seconds after
SIGTERM) and a stretch of S3 `500`s. Two of the janitor's log lines:

```
02:14:07 DELETE ci/builder/manifests/sha256:5b1d0c…  -> connection reset by peer
02:14:09 DELETE ci/builder/manifests/sha256:5b1d0c…  -> 404 MANIFEST_UNKNOWN
02:31:40 DELETE ci/runner/manifests/sha256:9e02af…   -> 202 Accepted
```

Both digests still have tags pointing at them.

Please fix Quayside (`/app`; start with `/app/README.md`) so this can't happen again:

- When a digest DELETE answers `202`, the manifest and every tag that pointed at it are gone.
- A DELETE interrupted at any point, by the replica dying or by S3 failing partway, leaves things
  in a state the janitor's retries (see the README) can finish.
- Once the requests involved have finished, no tag shown by `tags/list` fails to pull. This has to
  hold with both replicas taking traffic: promotion and rollback jobs re-tag existing digests,
  old ones included, through either replica whenever they like, and the janitor knows nothing about
  them.
- Everything else keeps working as documented: pushes, pulls by tag and by digest, re-tagging,
  pushing a deleted manifest again, `tags/list`, `404` for a digest the registry doesn't know.
  Deleting a digest never removes a tag that points at a different digest.
- The bucket layout is read by the GC and the audit and already holds production data written by
  earlier versions; it has to stay compatible.

The broken tags already in production will be cleaned up by hand, so no migration is needed.

Each replica is run as

```
python3 -m quayside --listen HOST:PORT --s3-endpoint URL --bucket NAME --replica-id ID
```

with `PYTHONPATH=/app`, from its own empty working directory. Replicas share nothing but the
bucket: no shared disk, no other AWS service, and every AWS call they make goes through
`--s3-endpoint`. Change whatever you need under `/app`, but keep that command line and the HTTP
API.
