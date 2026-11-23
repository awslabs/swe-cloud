# cachesvc

Our Python port of [bazel-remote](https://github.com/buchgr/bazel-remote)'s disk cache with the
S3 proxy backend, trimmed to what the build fleet uses: an LRU disk cache on each replica, one
shared S3 bucket behind all replicas, and a small JSON/HTTP API in front of it that our Bazel
gateway speaks. Code layout follows upstream: `cachesvc/disk.py` is `cache/disk`,
`cachesvc/s3proxy.py` is `cache/s3proxy`.

## Running

    PYTHONPATH=/app python3 -m cachesvc

All configuration comes from the environment:

| variable | default | meaning |
|---|---|---|
| `LISTEN` | `127.0.0.1:9090` | address to serve HTTP on |
| `S3_ENDPOINT` | `http://localhost:4566` | S3 endpoint (path-style) |
| `BUCKET` | `bazel-cache` | bucket shared by all replicas |
| `S3_PREFIX` | empty | optional key prefix inside the bucket |
| `DISK_DIR` | `/var/cache/cachesvc` | this replica's disk cache; persistent, private to the replica |
| `DISK_MAX_BYTES` | 1 GiB | disk cache size bound (LRU) |
| `S3_UPDATE_TIMESTAMPS` | false | copy an object onto itself inline on every S3 download, any kind (upstream `--s3.update_timestamps`) |
| `CAS_REFRESH_AGE` | 0 | seconds; see below |
| `NUM_UPLOADERS` | 4 | background S3 workers |
| `MAX_QUEUED_UPLOADS` | 1000 | bound on queued background S3 work |
| `PROXY_CONTAINS_WORKERS` | 16 | concurrent S3 lookups for FindMissing local misses |

Credentials and region come from the usual `AWS_*` variables.

`CAS_REFRESH_AGE` (upstream `--s3.cas_refresh_age`): Copy CAS blobs found or fetched from S3 onto
themselves in the background once they are older than this, so lifecycle rules that expire by last
write keep them. 0 disables. Ignored with `S3_UPDATE_TIMESTAMPS`. Design note:
[docs/cas_refresh_age.md](docs/cas_refresh_age.md).

## HTTP API

Digests are `{"hash": "<sha256 hex>", "size": <bytes>}`.

| request | reply |
|---|---|
| `GET /healthz` | 200 once the replica is serving |
| `POST /findmissing` `{"digests": [...]}` | `{"missing": [...]}`: the digests the cache does not have. A digest reported present must be fetchable through `GET /cas` on any replica. |
| `PUT /cas/<hash>` raw bytes | 200 once `cas.v2/<hh>/<hash>` is in the bucket |
| `GET /cas/<hash>` | the bytes (local disk, else S3), or 404 |
| `PUT /ac/<hash>` `{"outputs": [digest, ...]}` | 200 once `ac/<hh>/<hash>` holds exactly that body |
| `GET /ac/<hash>` | the stored entry, or 404 |

The gateway behaves like Bazel: before it writes an action result it asks `findmissing` about every
output and uploads only what is reported missing, then `PUT /ac`. With `--remote_download_minimal`
the client does not download outputs after an AC hit; it fetches them later, if at all. While such
a build runs, it also asks `findmissing` about the outputs it holds only remotely, to make sure they
are still there before it relies on them (Bazel's lease extension). That build has no bytes to
upload: a digest reported missing there fails it with lost inputs, just like a deleted output, and
so does an error reply to that `findmissing`.

AC entries are never cached on a replica's disk: every replica answers `GET /ac` from the bucket,
so an entry that lifecycle removed is a 404 everywhere.

## Bucket layout and lifecycle

    ac/<hash[:2]>/<hash>        action results
    cas.v2/<hash[:2]>/<hash>    blobs

The bucket is bounded only by lifecycle Expiration rules, one per prefix, which count from each
object's LastModified. Nothing in cachesvc deletes objects.

```yaml
# Keep CAS blobs that are still in use alive under an S3 lifecycle rule that
# expires by last write. Expire ac/ at least this long before cas.v2/.
#  cas_refresh_age: 168h
```

## Tests

    cd /app && python3 -m pytest -q tests
