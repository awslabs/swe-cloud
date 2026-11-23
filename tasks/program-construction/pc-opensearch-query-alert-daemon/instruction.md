# Rebuild a scheduled query-alert daemon

`/reference/program` is a compiled daemon that runs search queries on a
schedule against an OpenSearch cluster. It reads a JSON configuration file and
a directory of rule files, evaluates conditions over each response, summarizes
matching hits and aggregation buckets, and sends alerts to JSON-lines files or
an Amazon SNS topic. After every execution it records a state document in the
cluster, and a restarted daemon resumes its schedule from that state.

Write your own implementation in `/app`, in any installed language, together
with an executable `/app/build.sh` that produces `/app/program`. The program
must behave like the reference for the scope described in
`/docs/interface.md`: invocation and environment variables, the AWS venue,
configuration and rule formats, outputs, state indices, exclusions and the
exact comparison rules the tests apply.

The guide orients you; it is not a complete specification. Run the reference
against the local AWS emulator (create an OpenSearch domain, an SNS topic and
an SQS queue as the guide shows) to settle how paths, buckets, conditions,
templates, schedules and state lookups actually behave. Use the reference only
by executing it; do not copy, disassemble or decompile it.

Tests run in a fresh environment that receives only `/app`: they run
`/app/build.sh` once, then start `/app/program` as an unprivileged user
against a new domain and topics. Nothing else from your session, including the
reference, documentation and AWS resources you created, is available there.
Python 3 with boto3 and standard shell tools are installed.

The task image's language/toolchain versions and Python packages are recorded in /etc/task-runtime.json before agent setup. Extra runtimes or packages installed by the agent harness are not guaranteed in the verifier. Only /app transfers; build.sh and program run offline as candidate with PATH=/usr/local/bin:/usr/bin:/bin and the AWS endpoint settings of this environment. Use the task-provided tools or bundle additional dependencies under /app and select them explicitly.
