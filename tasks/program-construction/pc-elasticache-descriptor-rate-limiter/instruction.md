# Rebuild a descriptor-based rate-limit service

`/reference/program` is a rate-limit service. You can run it but not read it.
Clients send it a domain and a list of descriptors (ordered key/value entries)
over HTTP (`POST /json`) or gRPC. It matches each descriptor against the limits
in its YAML configuration files and counts hits in fixed-window counters kept in
Valkey (Redis protocol). Then it reports, per descriptor and overall, whether
the request is within its limits.

Write your own implementation, in any installed language, so that `/app/program`
behaves like the reference. Put your sources and `/app/build.sh` under `/app`.
The build script must produce `/app/program` offline in a fresh copy of the
image. Only `/app` is kept. The reference, `/docs` and any storage you created
while exploring are not available when your program is graded. Use the reference
to observe behavior. Do not copy, decompile or wrap it.

Read `/docs/interface.md` first. It covers the invocation and environment
variables, how to create ElastiCache Valkey storage in the emulated AWS venue,
the configuration language, the request and response formats, how counters
appear in storage, what is graded and what is not, and how outputs are compared.
`/docs/rls_subset.proto` describes the gRPC messages.

The guide names the features but leaves out many of their consequences: matching
precedence, accounting edge cases, failure handling, reload behavior and storage
effects. Work these out by experimenting with the reference and inspecting
Valkey directly. Graders run your program with their own configuration and
storage, then compare its replies and Valkey counters with the reference's
behavior.

The task image's language/toolchain versions and Python packages are recorded in /etc/task-runtime.json before agent setup. Extra runtimes or packages installed by the agent harness are not guaranteed in the verifier. Only /app transfers; build.sh and program run offline as candidate with PATH=/usr/local/bin:/usr/bin:/bin and the AWS endpoint settings of this environment. Use the task-provided tools or bundle additional dependencies under /app and select them explicitly.
