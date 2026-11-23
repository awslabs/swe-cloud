# Rebuild the secret cache proxy

`/reference/program` is a local caching proxy for AWS Secrets Manager. Run as
`program sm start --config <file.toml>`, it listens on a loopback port and answers
token-protected HTTP GET requests (`/secretsmanager/get?secretId=...` and a path form such
as `/v1/<id>`) with secret values. It keeps those values in an in-memory cache governed by
a TTL, a size limit, staging labels, `refreshNow` and optional pre-fetching at startup.

Write your own implementation, in any installed language, that behaves the same way. Put
its source and an executable `/app/build.sh` under `/app`; the script must produce
`/app/program`, accepting the same command line, configuration, environment variables and
HTTP requests as the reference. Only `/app` is carried over to grading,
where `/app/build.sh` runs again in a fresh container. The reference, `/docs` and any AWS
state you create during development are not carried over.

Start with `/docs/interface.md`. It covers invocation, AWS endpoint and credentials,
configuration keys, scope, and exactly how outputs and AWS traffic are compared. The guide deliberately leaves the important rules open: how cache
entries are keyed, what happens on expiry, eviction, label moves, pre-fetch, service
failures, request parsing and exact status codes and bodies. Work these out by running
the reference against the AWS emulator that is already configured in the environment
(`AWS_ENDPOINT_URL`), creating and changing secrets with boto3, and watching both its
HTTP answers and the AWS calls it makes.

Use the reference only by running it. Do not copy, disassemble or decompile it.

The task image's language/toolchain versions and Python packages are recorded in /etc/task-runtime.json before agent setup. Extra runtimes or packages installed by the agent harness are not guaranteed in the verifier. Only /app transfers; build.sh and program run offline as candidate with PATH=/usr/local/bin:/usr/bin:/bin and the AWS endpoint settings of this environment. Use the task-provided tools or bundle additional dependencies under /app and select them explicitly.
