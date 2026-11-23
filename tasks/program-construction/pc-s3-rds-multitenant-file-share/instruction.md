# Rebuild a multi-tenant file-sharing server

`/reference/program` is a file-sharing HTTP server. Several local users share one
instance. Each user signs in with a password session or with their own API tokens. Users
create uploads, add files and hand out download links. File bytes go to S3 and
metadata goes to PostgreSQL, both provided here by the local AWS emulator (S3 and an RDS
PostgreSQL instance). The program also has offline CLI commands that provision users and
tokens, inspect and delete files, clean up expired data and export or import metadata
against the same database and bucket.

Write your own implementation, in any installed language, that behaves like the reference
for the admitted scope. Put the source under `/app` and an executable `/app/build.sh` that
produces `/app/program`. Only `/app` is carried over to a fresh verification environment.
The reference, the documentation and any AWS state you create during development are not
carried over. Use the reference only by running it. Do not copy or decompile it.

Read `/docs/interface.md` first. It gives the invocation, the TOML configuration and
endpoint setup, the HTTP endpoints and credentials in scope, the exclusions, and the
tables, objects and comparison rules that the tests observe.

Hidden tests run one or more tenants at once. They check HTTP responses, headers,
downloaded bytes and archives, CLI output, and the Postgres rows and S3 objects your
program leaves behind, including after refused operations. Run the reference and find
out how it behaves across the whole admitted scope: who may manage which upload and
what each caller's listings, statistics and tokens cover; per-user and per-file limits;
upload options and the feature flags that refuse or impose them; upload lifetime,
expiry and what the cleaning command removes; how objects are named, encrypted and
split into parts in S3; archives, streaming and one-shot downloads; administrator
accounts and the CLI login flow; and the file, user and metadata subcommands.

The task image's language/toolchain versions and Python packages are recorded in /etc/task-runtime.json before agent setup. Extra runtimes or packages installed by the agent harness are not guaranteed in the verifier. Only /app transfers; build.sh and program run offline as candidate with PATH=/usr/local/bin:/usr/bin:/bin and the AWS endpoint settings of this environment. Use the task-provided tools or bundle additional dependencies under /app and select them explicitly.
