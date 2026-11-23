# private-object-gateway: interface

`private-object-gateway` is a single executable (`program`) that serves an
S3-compatible HTTP API to several **tenant accounts** and stores everything in
one upstream S3 service (here the AWS emulator at `$AWS_ENDPOINT_URL`). Each
tenant signs requests with its own access key. The gateway checks the signature
and the bucket ownership itself, then forwards allowed requests upstream using
a single set of *backend* credentials. The upstream service performs no
per-tenant authorization, so all isolation happens in the gateway.

This guide covers the invocation syntax, the environment, the supported
operations and the externally visible storage layout. Response details,
error codes and edge-case results are not specified here. To learn them, run
the reference program and observe what it does.

## Environment

* AWS emulator: `AWS_ENDPOINT_URL` (for example `http://engines:4566`), region
  `us-east-1`, dummy credentials in `AWS_ACCESS_KEY_ID` /
  `AWS_SECRET_ACCESS_KEY`. These are the **backend** credentials. Tenants never
  use them.
* Only the S3 API of the emulator is used. No other AWS service and no engine
  are involved.
* Before starting the gateway, create two private buckets directly in the
  emulator with the backend credentials:
  * a **metadata bucket**, which holds per-bucket ownership/ACL records;
  * an **account bucket**, which holds the account store.

## Starting the gateway

```
program [global options] s3 [s3 options]
```

The process runs in the foreground until it is terminated (SIGTERM/SIGINT).

Global options used in scope:

| option | meaning |
|---|---|
| `--port ADDR` (`-p`) | listen address, e.g. `127.0.0.1:7070` (default `:7070`) |
| `--access KEY` / `--secret SECRET` (`-a`/`-s`) | root (administrator) credentials of the gateway. These must differ from every tenant key. |
| `--region R` | region that clients sign for (default `us-east-1`) |
| `--quiet` (`-q`) | do not print per-request log lines on stdout |
| `--s3-iam-access KEY` / `--s3-iam-secret SECRET` | backend credentials used to read and write the account store |
| `--s3-iam-region R` | region of the account store (default `us-east-1`) |
| `--s3-iam-bucket NAME` | account bucket |
| `--s3-iam-endpoint URL` | S3 endpoint of the account store |

`s3` subcommand options used in scope:

| option | meaning |
|---|---|
| `--endpoint URL` | upstream S3 endpoint (`$AWS_ENDPOINT_URL`) |
| `--use-path-style` | address upstream buckets in the URL path |
| `--region R` | upstream region (`us-east-1`) |
| `--access KEY` / `--secret SECRET` | backend credentials for data and metadata |
| `--meta-bucket NAME` | metadata bucket. It must already exist. This option is required for this task. |
| `--disable-data-integrity-check` | compute upstream request checksums only when the operation requires them. Always pass this option in this task, because the emulator is reached over plain HTTP. |

The account-store client has no path-style switch. Give `--s3-iam-endpoint` an
**IP-address** URL (resolve the emulator host name first, e.g.
`http://172.18.0.2:4566`) so that the account bucket is addressed in the path.

Example:

```bash
IP=$(python3 -c 'import socket;print(socket.gethostbyname("engines"))')
program --port 127.0.0.1:7070 --access rootkey --secret rootsecret --quiet \
  --s3-iam-access "$AWS_ACCESS_KEY_ID" --s3-iam-secret "$AWS_SECRET_ACCESS_KEY" \
  --s3-iam-region us-east-1 --s3-iam-bucket my-accounts \
  --s3-iam-endpoint "http://$IP:4566" \
  s3 --endpoint "$AWS_ENDPOINT_URL" --use-path-style --region us-east-1 \
     --access "$AWS_ACCESS_KEY_ID" --secret "$AWS_SECRET_ACCESS_KEY" \
     --meta-bucket my-meta --disable-data-integrity-check
```

Wait until the listen port accepts connections before you send requests.
Startup and admin requests with either prerequisite bucket missing are compared
as well: the gateway's exit status, whether it listens, the admin client's exit
status and the upstream buckets afterwards.

## Administration client

Use the same executable as an administration client. It talks to the running
gateway's own port and must be signed with the gateway's root credentials (or
with the keys of an `admin`-role account):

```
program admin --access ROOTKEY --secret ROOTSECRET --endpoint-url http://127.0.0.1:7070 SUBCOMMAND ...

  create-user --access KEY --secret SECRET --role ROLE [--user-id N] [--group-id N] [--project-id N]
  update-user --access KEY [--secret SECRET] [--role ROLE] [--user-id N] [--group-id N] [--project-id N]
  delete-user --access KEY
  list-users
  list-buckets
  change-bucket-owner --bucket NAME --owner KEY
  create-bucket --bucket NAME --owner KEY
```

* The admin client exits with status 0 on success and a nonzero status on any
  failure (the diagnostic goes to stderr; its wording is not graded). Explore
  which requests the gateway refuses.
* Create accounts **one at a time** (serially). An account created through the
  running gateway can be used immediately. Concurrent account changes, and
  changes made to the account store behind a running gateway, are out of scope.
* The account store may also be prepared before the gateway starts by writing
  `users.json` (layout below) into the account bucket directly; records may
  omit the numeric members.
* Roles in scope are `userplus`, `user` and `admin`. Most tenants have role
  `userplus`, which allows a tenant to create its own buckets; what a `user`-role
  account may do (including after `update-user` changes a role) is compared.
  An `admin`-role account is graded only as a signer of admin-client requests;
  S3 requests signed by root or admin identities are not graded.
* `update-user` changes only the members it is given; `delete-user` removes the
  account. `change-bucket-owner` hands an existing bucket to another account;
  `create-bucket` creates a bucket on behalf of an account. Their effects are
  observed in the account store, the ownership records and later tenant requests.
* `list-users` prints a table with one row per account. Each row holds, separated
  by whitespace, the access key, role, user ID, group ID and project ID. Header
  and separator lines are not graded; secrets are never printed.
* `list-buckets` prints a table with one row per upstream bucket other than the
  metadata bucket: the bucket name, then the owner's access key when the bucket
  has one. Header and separator lines are not graded.

## Tenant S3 API in scope

Tenants use any SigV4 S3 client (for example boto3 with
`endpoint_url=http://127.0.0.1:7070`, path-style addressing, region
`us-east-1`). The supported operations are:

* Buckets: `ListBuckets`, `CreateBucket`, `HeadBucket`, `DeleteBucket`,
  `GetBucketLocation`
* Bucket configuration: `Get/PutBucketVersioning`,
  `Get/Put/DeleteBucketPolicy`, `GetBucketPolicyStatus`, `Get/PutBucketAcl`,
  `Get/Put/DeleteBucketOwnershipControls`, `Get/Put/DeleteBucketCors`,
  `Get/Put/DeleteBucketWebsite`, and the reply to `PutBucketTagging`
* Objects: `PutObject`, `GetObject`, `HeadObject`, `DeleteObject`,
  `DeleteObjects`, `CopyObject`, `GetObjectAttributes`,
  `Get/Put/DeleteObjectTagging`
* Listings: `ListObjectsV2`, `ListObjects` and `ListObjectVersions`
* Multipart uploads: `CreateMultipartUpload`, `UploadPart`, `UploadPartCopy`,
  `ListParts`, `ListMultipartUploads`, `CompleteMultipartUpload` and
  `AbortMultipartUpload`

A bucket belongs to the account that created it (or that the administrator
assigned it to). Object requests carry an optional `Content-Type`,
`Cache-Control`, `Content-Disposition`, `Content-Encoding`, `x-amz-meta-*`
user metadata, `x-amz-tagging` and an `x-amz-checksum-sha256` value. Reads
may use `Range`, `If-Match`, `If-None-Match`, `versionId` and the
`response-*` override parameters; a `PUT` may carry `If-None-Match: *`;
copies may carry `x-amz-copy-source-if-*`, `x-amz-copy-source-range`, the
metadata directive and the tagging directive. Object listings support the
usual prefix, delimiter, start-after, marker and paging parameters. Do not use
the pagination parameters of `ListBuckets` (`max-buckets`,
`continuation-token`, `prefix`).

Bucket policies and bucket ACLs can open a bucket to other tenants or to
unsigned requests. Policies name tenants by access key in `Principal`
(`{"AWS": [...]}` or `"*"`). ACLs are set with a canned `x-amz-acl` header or
an `AccessControlPolicy` body naming grantees by access key; `CreateBucket`
may carry a canned ACL together with `x-amz-object-ownership`. Which requests a
policy or grant allows or refuses, and which policies and ACLs are rejected,
are compared.

Requests are signed the way boto3 1.35 signs them: SigV4 in the
`Authorization` header with `x-amz-content-sha256`, over plain HTTP. Requests
without any signature also arrive, and so do presigned URLs (SigV4 in the
query string, signed for the `host` header only, used before they expire).
`aws-chunked` streaming uploads are not used.

What is in scope for bucket names: names that do not yet exist upstream, names
the gateway itself already manages, and the metadata bucket's name. Creating a
bucket whose name exists upstream without an ownership record (including the
account bucket) is out of scope; requests for such buckets are in scope.
Reusing the name of a deleted bucket, and `change-bucket-owner` for a bucket
that does not exist, are out of scope.

Out of scope: IAM policy documents, STS/OIDC/LDAP/Vault identities, ACL grant
headers (`x-amz-grant-*`) and object ACLs, `Content-MD5`, Object Lock and
retention, lifecycle, encryption, replication, request-payment and other
bucket configuration calls not listed above, CORS preflight (`OPTIONS`) and
website endpoints, presigned URL expiry, bucket notifications, metrics, the
web UI, and the other storage backends and subcommands (`posix`, `scoutfs`,
`azure`, `plugin`, `iam`, `test`, `utils`).

## Externally visible storage

Tests inspect the emulator directly with the backend credentials:

* **Object data** for a tenant bucket `B` lives in the upstream bucket `B`
  under the same key, byte for byte. Object headers, user metadata, tags,
  checksums, object versions and delete markers, open multipart uploads, and
  the bucket's versioning status and ownership controls are observed on the
  upstream bucket as well.
* **Ownership/ACL record** for bucket `B` is the object
  `vgw-meta-acl-B` in the metadata bucket. It is a JSON document whose
  `"Owner"` member is the owning account's access key. A `"Grantees"` array
  lists the grants, each with `"Access"`, `"Type"` and `"Permission"` members.
* **Bucket policy** for bucket `B` is the object `vgw-meta-policy-B` in the
  metadata bucket: the policy document (compared as decoded JSON).
* **CORS and website configurations** for bucket `B` are the objects
  `vgw-meta-cors-B` and `vgw-meta-website-B` in the metadata bucket, holding the
  configuration XML. The tests check only that the configured values appear in it.
* **Account store**: the object `users.json` in the account bucket. It is a
  JSON document of the form
  `{"accessAccounts": {"<access key>": {"access": ..., "secret": ...,
  "role": ..., "userID": ..., "groupID": ..., "projectID": ...}, ...}}`.

JSON records are compared after decoding. Member order and whitespace do not
matter. Strings, numbers and booleans are distinct types, and numbers are
compared by value (`7` and `7.0` are equal). Bucket listings are compared as
sets of names. The metadata bucket itself is internal to the gateway. Besides
`users.json`, the account bucket holds nothing else; besides the records listed
above, the metadata bucket holds nothing else.

## Observation rules

* Results are observed as S3 responses: HTTP status, the S3 error `Code` element
  (for HEAD requests and `304` replies, only the status, since there is no body),
  returned bytes, `Content-Type`, `Content-Range`, the standard headers listed
  above, user metadata, `ETag` (compared with the one the emulator reports for
  the stored object), version IDs, checksums, tag sets, the bucket, key, version,
  part and upload listings, and the per-key results of `DeleteObjects`. Error
  messages, request IDs, `Owner` and `Initiator` elements and timestamps are not
  graded.
* Upstream state is observed independently in the emulator after every step,
  including after refused requests.
* Gateway startup is observed through its exit status and whether it listens.
  The admin client is observed through its exit status, its `list-users` and
  `list-buckets` rows, the account store and the ownership records.
* `--help` text, `--version` output, log formatting and timing are not graded.
* Every request a tenant signs is judged on that tenant's own identity. A
  request with a bad signature never acts on behalf of any account.
