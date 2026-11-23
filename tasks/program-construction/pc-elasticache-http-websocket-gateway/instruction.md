# Rebuild an HTTP and WebSocket gateway for a Redis-protocol cache

`/reference/program` is a pinned build of `redis-gateway`. It reads a JSON configuration
file, turns HTTP request paths such as `/SET/key/value` into commands for a Redis-protocol
server, and returns the replies in a format the client selects: JSON, JSONP, raw protocol,
MessagePack, or a plain value with a chosen content type. It also enforces ordered
access-control rules, streams publish/subscribe messages over chunked HTTP, and serves
commands over WebSockets. Here the Redis-protocol server is a Valkey cache that you
provision through the Amazon ElastiCache API of the local AWS endpoint.

Write your own implementation, in any installed language, that behaves the same way. Put
its source and an executable `/app/build.sh` under `/app`. Running the script must produce
`/app/program`, which is started as `/app/program CONFIG.json` and keeps serving until it
is sent a signal.

Read `/docs/interface.md` first. It covers invocation, configuration keys, how to reach the
cache, request syntax, output formats, access control, what is in and out of scope, and how
responses are compared. It does not spell out every mapping, precedence rule or edge case.
Run experiments against `/reference/program` and inspect the cache yourself to find out
what it does.

Only `/app` carries over to grading, which runs in a fresh environment without the reference
program, the docs or any AWS state you created. Use the reference only by running it; do not
copy, disassemble or decompile it. Python 3 with `boto3`, `redis`, `msgpack` and
`websocket-client` is installed, along with standard shell tools.

The task image's language/toolchain versions and Python packages are recorded in /etc/task-runtime.json before agent setup. Extra runtimes or packages installed by the agent harness are not guaranteed in the verifier. Only /app transfers; build.sh and program run offline as candidate with PATH=/usr/local/bin:/usr/bin:/bin and the AWS endpoint settings of this environment. Use the task-provided tools or bundle additional dependencies under /app and select them explicitly.
