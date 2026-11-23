# Rebuild a trace store with alias-based index rollover

`/reference/program` is a Linux executable that keeps distributed-tracing data in
OpenSearch. It has two faces. `program es-rollover init|rollover|lookback URL …` manages
numbered indices, read/write aliases and index templates. `program --config file:…`
runs a server that accepts OpenTelemetry (OTLP/HTTP JSON) spans, writes span and
service documents through the write aliases, and answers a JSON query API through the
read aliases.

Build your own implementation that behaves the same way. Write the source under `/app`
and an executable `/app/build.sh` that produces `/app/program`. Only `/app` is carried to
a fresh verification machine: the reference, `/docs` and any cluster state you created
will not be there, and the build runs without network access. You may use any installed
language; Python with boto3, `curl` and standard shell tools are available.

Start with `/docs/interface.md`. It describes how to obtain an OpenSearch domain from
the local AWS endpoint, how to invoke both faces, the admitted configuration, flags and
inputs, the exclusions, and how results are compared. Then explore the reference
against a real domain. Look at what each action does to indices, aliases and templates,
how the actions and a running server interact, what the stored documents look like, and
how lookups and searches answer. Use the reference only by running it; do not copy,
disassemble or decompile it.

Verification runs your program against a fresh OpenSearch domain. It checks exit
statuses, cluster state after each step, stored documents and query responses, including
failure cases and multi-step sequences.

The task image's language/toolchain versions and Python packages are recorded in /etc/task-runtime.json before agent setup. Extra runtimes or packages installed by the agent harness are not guaranteed in the verifier. Only /app transfers; build.sh and program run offline as candidate with PATH=/usr/local/bin:/usr/bin:/bin and the AWS endpoint settings of this environment. Use the task-provided tools or bundle additional dependencies under /app and select them explicitly.
