Build a command-line program, `sql-version-runner`, that behaves like the reference
executable at `/reference/program`.

The program applies versioned SQL migration files to a PostgreSQL database and keeps
the current version, and whether a migration was interrupted, in a bookkeeping table.
Migrations come from a local directory or from an S3 bucket prefix. It moves the
database up, down or to a chosen version, can force the recorded version, report it or
drop the database's contents, and creates new empty migration pairs with sequential or
timestamp versions.

Read `/docs/interface.md` first. It describes invocation, the commands, file naming,
database URL parameters, the AWS venue (S3 and RDS PostgreSQL behind
`AWS_ENDPOINT_URL`), what is out of scope, and exactly which effects are compared.
The guide is an orientation, not a full specification. Learn the details by running
the reference against real RDS databases and S3 prefixes: how versions are ordered and
selected, how partial or failed runs leave the bookkeeping table, how missing files,
limits, prompts and URL parameters behave, and what concurrent invocations do.

Put your source code in `/app` and write `/app/build.sh`, which must produce the
executable `/app/program`. Any installed language is fine; Python with boto3 and
psycopg is available. Only `/app` is kept: verification happens in a fresh
environment, without the reference, the docs or any AWS state you created. Use the
reference only by running it; do not copy, unpack or decompile it.

The task image's language/toolchain versions and Python packages are recorded in /etc/task-runtime.json before agent setup. Extra runtimes or packages installed by the agent harness are not guaranteed in the verifier. Only /app transfers; build.sh and program run offline as candidate with PATH=/usr/local/bin:/usr/bin:/bin and the AWS endpoint settings of this environment. Use the task-provided tools or bundle additional dependencies under /app and select them explicitly.
