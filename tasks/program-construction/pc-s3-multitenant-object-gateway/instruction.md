# Private-bucket object gateway

Build `/app/program`, a multi-tenant S3 gateway. Tenants sign requests with
their own SigV4 access keys. The gateway checks those signatures and enforces
bucket ownership itself, then forwards allowed requests to one upstream S3
service (the AWS emulator at `$AWS_ENDPOINT_URL`) using a single set of backend
credentials. The upstream service cannot tell tenants apart.

A reference build is installed at `/reference/program`. Run it against the
emulator to learn its behaviour. `/docs/interface.md` gives the invocation,
the environment, the operations in scope, the storage layout and the
comparison rules. Your program must accept the same command lines for the
long-running `s3` gateway and for the `admin` client (`create-user`,
`update-user`, `delete-user`, `list-users`, `list-buckets`,
`change-bucket-owner`, `create-bucket`).

Graded behaviour:

- startup, and the account store and ownership records the admin client
  maintains (accounts, roles, bucket listings and owner changes);
- bucket creation and deletion, ownership records, HeadBucket and per-tenant
  ListBuckets;
- object PUT/GET/HEAD/DELETE, batch delete, copies, ranges, conditional
  requests, headers, checksums, tags and listings, with bytes stored verbatim
  upstream;
- multipart uploads, bucket versioning and object versions;
- bucket policies, ACLs, ownership controls, CORS and website configurations,
  and the access that policies, grants and presigned URLs give;
- requests aimed at another tenant's bucket, an internal, unregistered or missing
  bucket, or carrying a bad, unknown or absent signature, and what stays
  unchanged upstream;
- multi-tenant sequences, accounts added later, and a restart over existing state.

Tests start your gateway with fresh metadata and account buckets. They bootstrap
tenants through your admin client, drive the tenants with boto3 and plain HTTP
(presigned URLs), and inspect the emulator directly.

Write your source under `/app` and an executable `/app/build.sh` that produces
`/app/program`. Only `/app` carries over to grading; the reference, docs and
development emulator state do not. Any installed language may be used; Python
3.12 with boto3 is available. Do not copy or decompile the reference.

The task image's language/toolchain versions and Python packages are recorded in /etc/task-runtime.json before agent setup. Extra runtimes or packages installed by the agent harness are not guaranteed in the verifier. Only /app transfers; build.sh and program run offline as candidate with PATH=/usr/local/bin:/usr/bin:/bin and the AWS endpoint settings of this environment. Use the task-provided tools or bundle additional dependencies under /app and select them explicitly.
