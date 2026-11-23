# Rebuild a declarative PostgreSQL schema tool

`/reference/program` is a command-line tool that keeps PostgreSQL databases in sync
with desired states written in an HCL schema language. It inspects live databases,
plans the DDL between two states and applies that plan, and its project file can take
database passwords from AWS Secrets Manager or Systems Manager Parameter Store. Build
your own program with the same observable behavior.

Start with `/docs/interface.md` (invocation, environment, admitted commands and flags,
project files, comparison rules and exclusions) and `/docs/hcl-schema.md` (the schema
language). The guides describe the surface; the reference shows the exact behavior.
Run it against RDS PostgreSQL instances, secrets and parameters that you create on the
AWS-compatible endpoint, and compare its plans, inspection output, exit statuses and
resulting database catalogs with yours. Pay attention to how states are normalized,
how plans are ordered and combined, what is left behind when something fails, and
how project environments are evaluated.

Write your source under `/app` together with an executable `/app/build.sh` that
produces `/app/program`. Any installed language is fine; Python with boto3, psycopg
and python-hcl2 is available. Only `/app` is kept: grading happens in a fresh
environment without the reference, the guides or any AWS or database state you
created, and runs `/app/program` against new databases and AWS resources. Use the
reference only by executing it; do not copy, decompile or wrap it.

The task image's language/toolchain versions and Python packages are recorded in /etc/task-runtime.json before agent setup. Extra runtimes or packages installed by the agent harness are not guaranteed in the verifier. Only /app transfers; build.sh and program run offline as candidate with PATH=/usr/local/bin:/usr/bin:/bin and the AWS endpoint settings of this environment. Use the task-provided tools or bundle additional dependencies under /app and select them explicitly.
