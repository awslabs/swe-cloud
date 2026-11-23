Build `/app/program`, a long-running HTTP service called `keyspace-gauges`. It connects
to a Valkey (Redis-protocol) server and serves Prometheus gauges about selected keys,
key counts, key groups, streams with their consumer groups and consumers, and the
results of user Lua scripts. It must behave like the reference executable at
`/reference/program`.

Start with `/docs/interface.md`. It covers invocation, flags and environment
variables, the key-list syntax, the `/metrics`, `/scrape` and `/health` endpoints,
the in-scope metric families with their labels, how to provision a Valkey
replication group through the local AWS endpoint, and the rules for comparing
output. The guide leaves many consequences unstated, for example:
- how values are classified and sized
- what happens to missing, empty or oddly named keys
- how stream identifiers and absent group fields are reported
- how key groups are named and overflowed
- how script outcomes affect a collection

Work those out by running the reference against data you seed yourself. Use the
reference only by executing it; do not copy, disassemble or decompile it.

Put your source code in `/app` together with an executable `/app/build.sh`. The
script must produce `/app/program`. Any installed language may be used. Python with
`boto3`, `redis` and `prometheus_client`, and standard shell tools, are available.

Grading happens in a fresh environment. Only `/app` is carried over: no reference,
docs or development data. Your build script runs there, and then a test suite starts
your service against newly provisioned servers. It compares the scraped metrics with
the reference's behavior. Only the families and observation rules described in the
guide are graded.

The task image's language/toolchain versions and Python packages are recorded in /etc/task-runtime.json before agent setup. Extra runtimes or packages installed by the agent harness are not guaranteed in the verifier. Only /app transfers; build.sh and program run offline as candidate with PATH=/usr/local/bin:/usr/bin:/bin and the AWS endpoint settings of this environment. Use the task-provided tools or bundle additional dependencies under /app and select them explicitly.
