# strata commit protocol (version index)

Our object stores have no atomic rename-if-not-exists or put-if-not-exists we can rely on, so
concurrent writers coordinate through an external **version index** in DynamoDB. The index is a
key-value store that supports put-if-not-exists. It supplements but does not replace the manifests
in object storage: a reader unaware of the index can still read a dataset straight from the
bucket, but may observe a version up to one commit behind the true latest version.

## Layout

Bucket `STRATA_BUCKET` (versioning enabled). Dataset `D` keeps its manifests under
`D/_versions/`:

| key | what |
|---|---|
| `D/_versions/N.manifest-<uuid>` | a committer's staged manifest for version N (unique per attempt) |
| `D/_versions/N.manifest` | the canonical manifest of version N |

Table `STRATA_TABLE`, one row per (dataset, version):

| attribute | type | meaning |
|---|---|---|
| `base_uri` | S, HASH | `s3://<bucket>/<dataset>` |
| `version` | N, RANGE | dataset version |
| `path` | S | the staging key while the version is pending, the canonical key once finalized |
| `size` | N | manifest size in bytes |
| `committer` | S | the process that last wrote the row |
| `generation` | S, optional | S3 VersionId of the manifest object, used for integrity checks |

All index reads are strongly consistent (`ConsistentRead`).

## Commit process

1. **Stage manifest**: `PUT D/_versions/N.manifest-<uuid>`
   - Write the new manifest under a unique path determined by a new UUID.
   - The staged manifest is not yet visible to readers.
2. **Commit to the index**: put-if-not-exists `(base_uri, N) -> D/_versions/N.manifest-<uuid>`
   - `attribute_not_exists(base_uri) AND attribute_not_exists(version)`.
   - The commit is effectively complete after this step.
   - If the condition fails, another writer has committed this version: report a conflict.
3. **Finalize in the object store**: `COPY D/_versions/N.manifest-<uuid> -> D/_versions/N.manifest`
   - Makes the manifest discoverable by readers unaware of the index.
4. **Update the index pointer**: `(base_uri, N) -> D/_versions/N.manifest`, then delete staging.

**Fault tolerance.** If the writer fails after step 2 but before step 4, the index and the object
store are temporarily out of sync. Readers detect this condition and complete the
synchronization themselves; they do not wait for the writer. If synchronization fails, the reader
refuses to load the version, to keep the dataset portable.

## Reader process

1. **Query the index**: `GET (base_uri, N)` (or the highest version for `--latest`).
   - If `path` is the canonical key, the version is finalized: check the row against the canonical
     object (HEAD) and refuse the version if they disagree.
   - If `path` is a staging key, synchronization is required.
2. **Synchronize to the object store**: copy staging to the canonical key. This operation is
   idempotent; any number of readers and writers may perform it.
3. **Update the index** to the canonical key. Future readers will see the synchronized state.
4. **Return the canonical location** — path, size, and the generation this process observed.

## Index rows are claims about objects

The index is what readers trust. Readers of every strata release still deployed check a finalized
row against the object at the canonical key before they use it, and refuse the version when the
two disagree. Whatever a row records about a finalized version therefore has to be true of the
object a reader finds at the canonical key.

## Object-store requirements

Plain PUT, COPY, HEAD, GET, DELETE and LIST on a versioning-enabled bucket. The protocol must not
depend on conditional creates or copy preconditions: the gateways in front of our stores answer
any request carrying `If-None-Match` or an `x-amz-copy-source-if-*` header with
`501 Not Implemented`.
