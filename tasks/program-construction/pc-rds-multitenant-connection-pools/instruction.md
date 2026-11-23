# Rebuild a multi-tenant PostgreSQL connection pooler

`/reference/program` is a PostgreSQL wire-protocol connection pooler. It reads a TOML
configuration file, accepts PostgreSQL clients and authenticates each one against a
statically configured tenant pool, chosen by the database name and user name the client
connects with. It forwards the client's SQL to a real PostgreSQL server over a bounded set
of reusable server connections. Each tenant has its own password, backend role, physical
database and pool size.

Write your own implementation and an executable `/app/build.sh` that produces
`/app/program`. It must accept the same configuration and serve clients the same way:
startup, password and TLS handling, simple and extended query forwarding including
`BEGIN`/`COMMIT`/`ROLLBACK` and cancel requests, transaction and session pooling,
per-tenant connection budgets and timeouts, server selection, read/write splitting and
sharding within a tenant's pool, the pooler's own routing commands, the admin console,
configuration reload and graceful shutdown. Tenants must stay apart. A client of one tenant
must never authenticate to or reach another tenant's database, and one tenant using up its
pool must not hold up another.

Start with `/docs/interface.md`. It covers invocation, the AWS venue (Amazon RDS for
PostgreSQL), the admitted configuration, what is out of scope and how results are
compared. Settle the open questions by experiment. Run the reference against databases
you create and watch what clients receive and what reaches each tenant's database:
refusals, errors, full pools, what a transaction holds on to, which server or shard a
query lands on, and what the admin console reports.

Any installed language may be used. Only `/app` is kept for grading; the reference, the
docs and your AWS resources are not. Use the reference by running it; do not copy or
decompile it.

The task image's language/toolchain versions and Python packages are recorded in /etc/task-runtime.json before agent setup. Extra runtimes or packages installed by the agent harness are not guaranteed in the verifier. Only /app transfers; build.sh and program run offline as candidate with PATH=/usr/local/bin:/usr/bin:/bin and the AWS endpoint settings of this environment. Use the task-provided tools or bundle additional dependencies under /app and select them explicitly.
