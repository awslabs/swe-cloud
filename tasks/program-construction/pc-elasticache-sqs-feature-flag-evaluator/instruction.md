# Rebuild the flag evaluator

`/reference/program` is a feature-flag evaluation service. It is a long-running HTTP
server. It reads flag definitions from a Valkey cache (Amazon ElastiCache) and sends an
evaluation event to an Amazon SQS queue for each single-flag evaluation. Your job is to
build your own implementation with the same observable behaviour.

Write your source code under `/app`, together with an executable `/app/build.sh`. Running
that script must produce the executable `/app/program`. Only `/app` is carried over to a
fresh grading environment. The reference program, `/docs` and any AWS resources you
create are not carried over, so the build must not depend on them. You may use any
installed language; Python 3.12 with boto3, redis and PyYAML is available, along with
standard shell tools. There is no network access beyond the local AWS endpoint.

Start with `/docs/interface.md`. It covers invocation, the configuration file, the AWS
endpoint and how results are compared. The guides it links describe the flag format,
the query language, the HTTP endpoints and the events. They describe the shape of the
service, not every rule. Run the reference against your own flags and contexts to learn
its reasons, error codes, status codes, ordering, metadata, query-language edge cases,
time-dependent flags and refresh behaviour. Only the admitted scope is graded.

Use the reference only by running it. Do not copy, disassemble or decompile it.
Grading starts your `/app/program` with fresh caches, queues and configuration files.
It then checks the HTTP responses, the store-refresh behaviour, the process exit status
and the SQS messages.

The task image's language/toolchain versions and Python packages are recorded in /etc/task-runtime.json before agent setup. Extra runtimes or packages installed by the agent harness are not guaranteed in the verifier. Only /app transfers; build.sh and program run offline as candidate with PATH=/usr/local/bin:/usr/bin:/bin and the AWS endpoint settings of this environment. Use the task-provided tools or bundle additional dependencies under /app and select them explicitly.
