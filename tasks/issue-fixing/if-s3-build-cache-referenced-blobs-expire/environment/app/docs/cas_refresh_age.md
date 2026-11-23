# s3proxy: cas_refresh_age keeps in-use CAS blobs alive

Design note for the change that added `CAS_REFRESH_AGE` (ported from upstream bazel-remote
PR #935, "s3proxy: add cas_refresh_age to keep in-use CAS blobs alive").

An S3 lifecycle rule that expires objects by last write can delete a CAS blob that a newer AC entry
still references. An existing blob is never uploaded again, so its age keeps growing while builds
keep finding it. With the AC dependency check disabled, Bazel then hits the missing blob and fails
the build. With the check enabled, the build stays correct, but every AC hit costs one S3 HEAD per
output.

This adds `cas_refresh_age` (default 0 = off). When `contains()` or `get()` finds a CAS blob older
than that age, the blob is copied onto itself in the background, which resets its last-modified
time. The age comes from the HEAD/GET that was already being made, so lookups gain no latency.
Unlike `update_timestamps`, the copy is never inline. Each live blob is copied at most about once
per period. A key that is already queued or being copied is skipped, so many concurrent lookups of
one hot blob produce one copy instead of a burst that S3 throttles with SlowDown. The option is
ignored when `update_timestamps` is set.

AC entries are never refreshed. A hot AC entry whose outputs are never downloaded would otherwise
outlive its blobs. Before uploading results, Bazel asks FindMissingBlobs about every output, so a
CAS blob is at most `cas_refresh_age` old in S3 when a new AC entry referencing it is written. The
blob either goes through `contains()`, or it is on local disk because it was just uploaded or
fetched via `get()`. A lifecycle config that expires `ac/` at least `cas_refresh_age` before
`cas.v2/` therefore never leaves an AC entry pointing at a deleted blob. The README example notes
this.

This is a different approach from upstream #625 and #923, which tie refreshing to
`update_timestamps` (#625 adds the inline copy to `contains()`; #923 refreshes on a skipped upload
of an existing blob).

Tests (`tests/test_s3proxy.py`, against a fake S3 client):

- An old CAS blob is refreshed, and a recent one isn't.
- AC entries are never refreshed.
- A blob that was just refreshed isn't copied again.
- Concurrent lookups while a copy is in flight produce one copy.
- The `get()` path refreshes.
- Setting the option to 0 disables refreshing.
