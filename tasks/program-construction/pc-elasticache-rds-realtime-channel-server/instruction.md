# Rebuild a realtime channel server

`/reference/program` is a Pusher-protocol realtime server. Clients subscribe to
channels over WebSocket; trusted backends publish and query through a signed HTTP
API on the same port. Apps (keys, secrets, limits, webhook targets) live in a
Postgres app table. Servers coordinate and cache through Valkey. Lifecycle and
client-event webhooks pass through an Amazon SQS queue and are then POSTed, signed,
to each app's webhook URLs.

Write your own implementation that behaves the same way. Put the source in `/app`,
plus an executable `/app/build.sh` that produces `/app/program`. Any installed
language may be used. Python with boto3, psycopg, redis and websockets is available.

Start with `/docs/interface.md`. It covers invocation and configuration, the AWS
resources (RDS Postgres, ElastiCache Valkey, SQS) the program talks to through the
local endpoint, which features are in scope, and how results are compared. The
guide is an outline. The exact responses, error frames, webhook timing, batching,
retry and queue handling, multi-server behaviour and cache effects are for you to
find out by running the reference against the same services. Use the reference to
observe behaviour only; do not copy, disassemble or wrap it.

Only `/app` is kept. Your program is rebuilt and tested on a fresh machine without
the reference, the docs or any AWS state you created. The tests start several
servers, Postgres databases, queues and webhook receivers. They then inspect
WebSocket frames, HTTP responses, webhook requests, SQS messages, Postgres tables
and Valkey keys.

The task image's language/toolchain versions and Python packages are recorded in /etc/task-runtime.json before agent setup. Extra runtimes or packages installed by the agent harness are not guaranteed in the verifier. Only /app transfers; build.sh and program run offline as candidate with PATH=/usr/local/bin:/usr/bin:/bin and the AWS endpoint settings of this environment. Use the task-provided tools or bundle additional dependencies under /app and select them explicitly.
