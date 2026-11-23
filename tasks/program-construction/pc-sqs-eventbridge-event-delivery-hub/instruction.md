# Rebuild the event-destination hub

`/reference/program` is a working event-destinations service. Tenants register
AWS SQS queues and EventBridge buses as destinations, each subscribed to topics
and optionally narrowed by a JSON filter. Events arrive through a REST API or an
SQS publish queue. They are fanned out through internal SQS queues, retried on
failure, and logged to PostgreSQL. Tenant and destination records live in Valkey.
The program also has a command line for migrations and configuration.

Your job is to write your own implementation of this program in `/app`.
Provide `/app/build.sh`, which must produce the executable `/app/program`.
Your program must behave like the reference: the same CLI output, REST responses and
error bodies, messages delivered to destinations, Valkey records and PostgreSQL
rows. You may use any installed language. Python with boto3, redis-py and
psycopg is available.

Start with `/docs/interface.md`. It covers invocation, configuration, how to
provision PostgreSQL and Valkey in the local AWS endpoint, what is in and out of
scope, and the rules used to compare your results. The other guides in `/docs`
give an overview of the API, routing and delivery. Many details are left for
you to discover. These include validation messages, record layouts, retry and
disable timing, and edge cases. Run the reference against the local AWS
endpoint to find them. Use the reference only by running it. Do not copy or
decompile it.

Only `/app` is kept for grading. Your program is built and tested in a fresh
environment, with fresh AWS resources, and without the reference or `/docs`.

The task image's language/toolchain versions and Python packages are recorded in /etc/task-runtime.json before agent setup. Extra runtimes or packages installed by the agent harness are not guaranteed in the verifier. Only /app transfers; build.sh and program run offline as candidate with PATH=/usr/local/bin:/usr/bin:/bin and the AWS endpoint settings of this environment. Use the task-provided tools or bundle additional dependencies under /app and select them explicitly.
