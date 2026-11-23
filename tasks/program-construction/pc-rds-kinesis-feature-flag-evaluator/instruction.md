# Rebuild the flag-evaluator server

`/reference/program` is a feature-flag and experimentation HTTP server. It stores
flags, variants, segments, constraints, distributions, tags and a per-change flag
history in PostgreSQL, evaluates flags for an entity context over a REST API, and
can write a data record of each evaluation to an Amazon Kinesis stream. Your job is
to build your own program with the same observable behavior.

Start with `/docs/interface.md`. It covers how the server is invoked and configured,
how to reach the AWS venue (RDS PostgreSQL and Kinesis at `http://engines:4566`), the
admitted features, and the exact rules used to compare results. The REST schema is in
`/docs/api.yaml`, and the Kinesis record format is in `/docs/kinesis-records.md`.
The guides describe the interface, not every rule behind it. Run the reference
against the venue to find out how it assigns variants, matches constraints, orders
and validates things, records history and packs records, and copy what it does.

Write your source under `/app` along with an executable `/app/build.sh` that
produces `/app/program`. You may use any installed language; Python 3 with boto3 and
psycopg is available. Only `/app` is carried over to grading, which runs in a fresh
environment with no reference program, docs or AWS state from your session. The
grader starts your server with environment settings from the guide, gives each
instance its own fresh database and stream, and checks API responses, stored
PostgreSQL rows and the records on the stream.

Use the reference only by running it. Do not copy, disassemble or decompile it.

The task image's language/toolchain versions and Python packages are recorded in /etc/task-runtime.json before agent setup. Extra runtimes or packages installed by the agent harness are not guaranteed in the verifier. Only /app transfers; build.sh and program run offline as candidate with PATH=/usr/local/bin:/usr/bin:/bin and the AWS endpoint settings of this environment. Use the task-provided tools or bundle additional dependencies under /app and select them explicitly.
