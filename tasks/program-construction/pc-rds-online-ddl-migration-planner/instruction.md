# Rebuild an online schema-migration planner for RDS PostgreSQL

`/reference/program` (`online-ddl-planner`) compares a live PostgreSQL database with a
desired schema, given as a directory of DDL files or as another database, and plans an
ordered, low-lock migration. Each statement has its own statement and lock timeouts and a
set of hazard types. `apply` runs a plan only if every hazard in it has been allowed, and
`dump` prints a schema as DDL. The databases are RDS PostgreSQL instances served by the
local AWS endpoint.

Implement your own program with the same observable behavior. Write its source under
`/app` and an `/app/build.sh` that produces the executable `/app/program`. You may use any
installed language. Python 3 with `boto3` and `psycopg` 3 is available, as are standard
shell tools. Only `/app` is carried over to a fresh verification environment. The
reference, the documentation and any databases you create are not.

Read `/docs/interface.md` first. It covers invocation, the AWS and database setup,
schema sources, options, output formats, the admitted scope and the exact rules for
comparing your output with the reference's. The guide does not spell out which hazards
each change raises, how plans order and phrase their statements, or how new tables are
laid out. Run the reference against your own RDS databases to find out. Use the reference
only by executing it. Do not copy, disassemble or decompile it.

Verification runs many schema changes against fresh databases. It checks plans in both
output formats, the hazard gate, catalogs and data after `apply`, failure effects, and
cleanup of temporary databases.

The task image's language/toolchain versions and Python packages are recorded in /etc/task-runtime.json before agent setup. Extra runtimes or packages installed by the agent harness are not guaranteed in the verifier. Only /app transfers; build.sh and program run offline as candidate with PATH=/usr/local/bin:/usr/bin:/bin and the AWS endpoint settings of this environment. Use the task-provided tools or bundle additional dependencies under /app and select them explicitly.
