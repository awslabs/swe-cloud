Build `/app/program`, a command-line extract-and-load runner that reproduces the observable
behavior of the reference executable at `/reference/program`.

The program reads PostgreSQL tables or queries and writes them to Apache Iceberg tables
registered in AWS Glue, to CSV or JSON-lines files in S3, or to another PostgreSQL database.
It is driven by command-line flags or by a replication YAML file of streams with defaults,
modes (`full-refresh`, `truncate`, `incremental`, `snapshot`), primary and update keys,
object-name templates, wildcards and target options.

Start with `/docs/interface.md` for invocation, environment, connection definitions, the
admitted scope and exactly how results are compared, and `/docs/replication.md` for the
replication file. The guides describe the inputs, not the consequences. How each mode treats
existing data, where incremental runs resume, how values are typed and rendered in each
target, how files are named, split and replaced, and what happens when source and target
shapes differ are for you to establish by running the reference against the AWS endpoint
and RDS PostgreSQL instances you create. Failures matter too: check exit status and what a
failed or refused run leaves behind.

Write your source under `/app` together with an executable `/app/build.sh` that produces
`/app/program`; grading runs `build.sh` in a fresh environment that contains only `/app`,
with no reference, documentation or AWS state from your session. Any installed language is
fine; Python with boto3, psycopg, pyarrow and pyiceberg is available. Use the reference only
by running it: do not copy, unpack or decompile it.

The task image's language/toolchain versions and Python packages are recorded in /etc/task-runtime.json before agent setup. Extra runtimes or packages installed by the agent harness are not guaranteed in the verifier. Only /app transfers; build.sh and program run offline as candidate with PATH=/usr/local/bin:/usr/bin:/bin and the AWS endpoint settings of this environment. Use the task-provided tools or bundle additional dependencies under /app and select them explicitly.
