# Quayside

Quayside is our in-house container registry: the manifest and tag part of the OCI distribution
API, stored in S3 with the same object layout as `distribution/distribution`'s S3 driver (we moved
off it in 2024 and kept the bucket). Layer blobs are served by a separate service and are not
handled here; manifests are accepted without checking that their layers exist.

## Deployment

- Two replicas (`registry-a`, `registry-b`) behind a round-robin load balancer. Any request can
  land on either replica, and consecutive requests from one client usually don't hit the same one.
- The replicas are stateless. The only state they share is the S3 bucket, and the only AWS access
  they have is that bucket through the endpoint they are started with (`--s3-endpoint`). There is
  no lock service, database or queue between them, and none is planned.
- Rolling deploys send SIGTERM and SIGKILL five seconds later; a replica can die in the middle of
  any request. Replicas are also restarted by the node agent if they stop answering `/healthz`.
- S3 has bad minutes: we see bursts of `500 InternalError`/`503 SlowDown` and occasional requests
  that take tens of seconds. boto3 retries a couple of times and then the call fails. A replica can
  also stall (CPU throttling, a long GC pause on the node) while it is in the middle of a request.

## Who calls it

- **CI** pushes every build as `PUT /v2/<repo>/manifests/<build-tag>`.
- **Promotion and rollback jobs** re-tag existing digests: `PUT /v2/<repo>/manifests/stable` with the
  manifest of a build that already exists, often an old one (a rollback re-tags whatever was live
  last week). They run whenever a release manager clicks, through whichever replica the load
  balancer picks, and know nothing about the janitor.
- **The janitor** (cron, `ops/janitor`, not in this repo) deletes expired builds by digest:
  `DELETE /v2/<repo>/manifests/sha256:...`. Each attempt has a 30 s client timeout. Any answer other
  than `202` or `404`, and any timeout, is retried every 2 s for up to 60 s, then it pages.
  `404` means "already gone" to the janitor; it does not retry it.

## HTTP API

| request | answer |
|---|---|
| `PUT /v2/<name>/manifests/<tag>` (manifest JSON body) | `201`, `Docker-Content-Digest`, `Location` |
| `PUT /v2/<name>/manifests/<digest>` | `201` (no tag) |
| `GET`/`HEAD /v2/<name>/manifests/<tag or digest>` | `200` with the manifest, or `404 MANIFEST_UNKNOWN` |
| `DELETE /v2/<name>/manifests/<digest>` | `202` once the manifest and every tag pointing at it are gone; `404 MANIFEST_UNKNOWN` if the registry doesn't know the digest |
| `DELETE /v2/<name>/manifests/<tag>` | `202`, removes just that tag |
| `GET /v2/<name>/tags/list` | `200 {"name": ..., "tags": [...]}` or `404 NAME_UNKNOWN` |
| `GET /healthz` | `200` when the replica is serving |

Errors use the distribution error body: `{"errors": [{"code": "MANIFEST_UNKNOWN", ...}]}`.
Deleting a digest removes the tags that point at that digest and nothing else. A manifest that was
deleted can be pushed again later and is then live again.

## Storage layout

```
docker/registry/v2/
  blobs/sha256/<hex[0:2]>/<hex>/data                       manifest bytes, content addressed
  repositories/<name>/_manifests/
    revisions/sha256/<hex>/link                             the repository has manifest sha256:<hex>
    tags/<tag>/current/link                                 what <tag> resolves to now
    tags/<tag>/index/sha256/<hex>/link                      every digest <tag> has pointed at
```

Every `link` object holds a digest string such as `sha256:0f1e...` and nothing else. A push writes
the manifest bytes, the revision link, the tag's index link and then its `current/link`, each as a
separate object.

This layout is shared with other tools that read the bucket directly, and the production bucket is
full of data written by every earlier Quayside version, so treat it as a contract:

- **The garbage collector** (`ops/gc`) keeps the blob of every revision whose `link` holds that
  revision's digest, and removes unreferenced blobs.
- **The nightly audit** (`ops/audit`) lists every `tags/*/current/link` and reports a tag as
  *broken* when the digest in it has no revision link holding that same digest. Those are the tags
  people see in `tags/list` and then fail to pull.

Both tools read only these keys, and treat a link that is missing or does not hold a well-formed
digest as absent. They ignore any other object in the bucket.

## Code map

```
quayside/
  __main__.py     CLI, starts one replica
  server.py       HTTP routing
  handlers.py     manifest/tag API handlers
  manifests.py    revision links (ManifestStore)
  tagstore.py     tag directories (TagStore)
  blobstore.py    content-addressed blobs, link read/write
  driver.py       S3 driver
  paths.py        the layout above
```

## Running it locally

The dev box runs the AWS emulator on `http://localhost:4566` (credentials `test`/`test`,
`us-east-1`); `bash /usr/local/bin/start_floci.sh` starts it if it isn't up.

```bash
python3 -c "import boto3; boto3.client('s3').create_bucket(Bucket='registry')"
cd /app
python3 -m quayside --listen 127.0.0.1:5001 --s3-endpoint http://localhost:4566 --bucket registry --replica-id a &
python3 -m quayside --listen 127.0.0.1:5002 --s3-endpoint http://localhost:4566 --bucket registry --replica-id b &

echo '{"schemaVersion":2,"mediaType":"application/vnd.oci.image.manifest.v1+json","config":{"mediaType":"application/vnd.oci.image.config.v1+json","digest":"sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a","size":2},"layers":[]}' > /tmp/m.json
curl -s -X PUT -H 'Content-Type: application/vnd.oci.image.manifest.v1+json' --data-binary @/tmp/m.json -i http://127.0.0.1:5001/v2/demo/app/manifests/v1
curl -s http://127.0.0.1:5002/v2/demo/app/tags/list
```

### Emulator differences from AWS S3

The emulator follows S3 semantics with three known deviations that only matter in dev:

- After a `PutObject` with `If-Match` is rejected with `412 PreconditionFailed`, the *next* request
  on the same keep-alive connection fails with a spurious `400`/`405`. Use a fresh client (or
  connection) after a 412. AWS does not do this.
- `PutObject` with `If-None-Match: *` answers and then breaks the next request the same way, and
  `CopyObject` with `CopySourceIfMatch` returns `400`. Don't rely on either.
- ETags are the MD5 of the body, as on AWS for single-part uploads: two writes of the same bytes
  get the same ETag.
