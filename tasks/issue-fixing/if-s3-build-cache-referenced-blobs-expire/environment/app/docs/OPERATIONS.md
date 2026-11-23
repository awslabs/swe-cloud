# cachesvc operations

## Deployment

- Replicas: two (`r1`, `r2`) behind the Bazel gateway, which pins a build's writes to one replica;
  AC reads can land on either. Each replica runs `python3 -m cachesvc` with its own `LISTEN` and
  `DISK_DIR`, and both share one bucket.
- `DISK_DIR` is a persistent volume per replica, sized far above the working set, so a blob that
  builds keep using stays on that replica's disk indefinitely.
- Replicas are restarted by deploys and by the node agent at any time of day, usually while
  serving traffic. The process gets SIGTERM; `DISK_DIR` survives, and the gateway retries requests
  that cannot connect to a replica while it is down. It does not retry a reply: whatever status a
  replica answers with, errors included, goes back to Bazel.
- The AC dependency check is off (it cost one S3 HEAD per output on every AC hit). Since
  `cas_refresh_age` we rely on the bucket never holding an AC entry whose outputs are gone.

Staging is time-compressed against production (days become seconds):

| setting | production | staging |
|---|---|---|
| lifecycle expiration `ac/` | 7 days | 20 s |
| lifecycle expiration `cas.v2/` | 14 days | 40 s |
| `CAS_REFRESH_AGE` | 168h | 7 |

Lifecycle is the only thing that ever deletes from the bucket. In staging it is applied by the
janitor, about once a second: any object whose LastModified is older than its prefix's expiration
is deleted. Bucket configuration (lifecycle, versioning, locks, policies) is owned by the storage
team; cachesvc never changes it.

## S3 as cachesvc sees it

Staging S3 sits behind the same request proxy as production traffic, so it behaves like the real
thing rather than like a local emulator:

- HeadObject takes about 1.5 s, CopyObject about 2 s (it is a server-side copy). Gets and puts of
  small objects are fast.
- S3 throttles: CopyObject on a key answers `503 SlowDown` during throttling episodes, sometimes
  several times in a row. botocore retries are disabled in cachesvc on purpose (see
  `make_client`), so throttling surfaces as a `ClientError`.
- Every S3 request is billed; the request bill is why we run a disk tier at all, and why the AC
  dependency check is off.

## What we hold the service to

- **FindMissing on blobs that are on local disk answers in under 250 ms (p99).** It is on the
  critical path of every action. Nothing on that path may wait for S3.
- **Cache hits served from local disk do not cost S3 requests.** Background S3 work for a blob
  that is in use is budgeted at roughly one request (a HEAD or a copy) per `cas_refresh_age`, plus
  the retries of throttled requests. Hot blobs are hit many times a second in production.
- **Concurrent lookups of one blob must not turn into concurrent copies of it.**
- **AC entries are never refreshed or rewritten by cachesvc.** An AC entry ages from the moment
  the client wrote it, and lifecycle removes it on schedule.
- **Storage is reclaimed.** A blob nobody has asked about (found, fetched, uploaded, or listed in
  an AC entry a replica served) for longer than the `cas.v2/` expiration plus one
  `cas_refresh_age` must be gone from the bucket. We have had the bucket grow without bound
  before, and the storage bill is watched.
- **No AC entry in the bucket references a blob that is not in the bucket.** Any replica may serve
  it, and with the dependency check off the client trusts it. This is the ordering argument in
  `docs/cas_refresh_age.md`: an AC entry lives for the `ac/` expiration after it is written, so
  every blob it lists must have been written or refreshed in S3 no more than
  (`cas.v2/` expiration − `ac/` expiration) before the entry was written — 20 s in staging —
  or the entry can outlive it.

## Logs

Each replica logs to stderr. `cachesvc.access` lines record every S3 call
(`S3 <op> <bucket> <key> <status>`), including `REFRESH` copies.
