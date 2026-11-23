# event-destination-hub — interface

`event-destination-hub` is an event-destinations service. Tenants register
destinations (AWS SQS queues and AWS EventBridge buses) subscribed to topics,
optionally narrowed by a JSON filter. Events published through a REST API or
an SQS publish queue are fanned out to every matching destination through
internal SQS queues. Each event and delivery attempt is logged to PostgreSQL.
Tenant and destination records are kept in Valkey.

The program is a single executable, `program`. It is configured only through
environment variables; the variables in scope are listed below.

## Invocation

```
program --version            print the version and exit
program --help               list commands
program migrate plan         show pending PostgreSQL and Valkey migrations
program migrate list         list all migrations and their status
program migrate apply --yes  apply all pending migrations (SQL, then Valkey)
program migrate verify       verify applied migrations
program migrate unlock --yes clear a stale migration lock
program config list          print the resolved configuration (secrets masked)
program serve                run the server (API, delivery and log workers) in the foreground
```

`serve` does not apply migrations. It refuses to start while any migration
is pending. Run `migrate apply --yes` against the same Valkey and PostgreSQL
first. `serve` runs until it receives SIGINT or SIGTERM, then shuts down
gracefully. Logs are JSON lines on stdout/stderr. Logs are diagnostic only and are
not part of the observable contract.

Readiness: `GET http://127.0.0.1:$API_PORT/healthz` returns 200 with a JSON
worker summary once the server is serving.

## Backing services (AWS venue)

The AWS API is reachable at `$AWS_ENDPOINT_URL` (region `us-east-1`, dummy
credentials). The service needs:

| Need | How to obtain it in the venue |
|------|-------------------------------|
| PostgreSQL (event/attempt log) | `rds.create_db_instance(Engine="postgres", DBName=..., MasterUsername=..., MasterUserPassword=...)`; wait for `available`; use `Endpoint.Address`/`Endpoint.Port` |
| Valkey (tenants, destinations, locks, retry schedule) | `elasticache.create_replication_group(Engine="valkey", NumCacheClusters=1, ...)`; wait for `available`; use `NodeGroups[0].PrimaryEndpoint` (no password) |
| Internal queues | SQS at the AWS endpoint. With `MQS_AUTO_PROVISION=true` (default) the server creates the delivery and log queues and their dead-letter queues itself |
| Publish queue (optional) | an SQS queue you create, named in `PUBLISH_AWS_SQS_QUEUE` |

## Configuration

Required:

| Variable | Meaning |
|----------|---------|
| `API_KEY` | admin bearer token for the REST API |
| `API_JWT_SECRET` | secret for tenant JWTs |
| `AES_ENCRYPTION_SECRET` | 16/24/32-byte key encrypting stored credentials |
| `REDIS_HOST`, `REDIS_PORT` | Valkey endpoint (`REDIS_PASSWORD` empty, `REDIS_DATABASE=0`) |
| `POSTGRES_URL` | `postgres://user:pass@host:port/db?sslmode=disable` |
| `AWS_SQS_REGION`, `AWS_SQS_ENDPOINT` | region and endpoint for the internal queues |
| `AWS_SQS_ACCESS_KEY_ID`, `AWS_SQS_SECRET_ACCESS_KEY` | credentials for the internal queues |
| `DISABLE_TELEMETRY=true` | always set; there is no outbound network |

Commonly used optional settings:

| Variable | Meaning |
|----------|---------|
| `API_PORT` | HTTP port (default 3333) |
| `TOPICS` | comma-separated list of allowed topics (unset: any topic) |
| `TOPICS_ALLOW_WILDCARDS` | allow `*` inside destination topic strings |
| `AWS_SQS_DELIVERY_QUEUE`, `AWS_SQS_LOG_QUEUE` | internal queue names; DLQs default to `<name>-dlq` |
| `MQS_AUTO_PROVISION` | create/manage the internal queues (default true) |
| `PUBLISH_AWS_SQS_QUEUE`, `PUBLISH_AWS_SQS_REGION`, `PUBLISH_AWS_SQS_ENDPOINT`, `PUBLISH_AWS_SQS_ACCESS_KEY_ID`, `PUBLISH_AWS_SQS_SECRET_ACCESS_KEY` | enable ingestion from an SQS publish queue |
| `PUBLISH_MAX_REDELIVERIES` | limit on redeliveries of failed publish-queue messages (-1 = unlimited) |
| `RETRY_SCHEDULE` / `RETRY_INTERVAL_SECONDS` / `MAX_RETRY_LIMIT` | automatic retry policy |
| `ALERT_CONSECUTIVE_FAILURE_COUNT`, `ALERT_AUTO_DISABLE_DESTINATION` | consecutive-failure threshold and automatic disabling |
| `LOG_BATCH_THRESHOLD_SECONDS`, `LOG_BATCH_SIZE` | log write batching. This is also the minimum retry delay |
| `MAX_DESTINATIONS_PER_TENANT` | per-tenant destination limit |
| `DESTINATIONS_AWS_EVENTBRIDGE_SOURCE` | `Source` used on EventBridge entries |
| `DESTINATIONS_INCLUDE_MILLISECOND_TIMESTAMP` | add a millisecond timestamp to delivery metadata |
| `DEPLOYMENT_ID` | namespace prefix shared by every store |
| `LOG_LEVEL` | log verbosity |

`program config list` prints every recognised setting with its resolved value.

## Scope

In scope:

- the command line above, including `migrate` and `config list` output.
- the REST API under `/api/v1`: tenants, tenant tokens, destinations, topics,
  destination-type schemas, publishing, event and attempt queries, and retry.
  See [api.md](api.md).
- `aws_sqs` and `aws_eventbridge` destinations. See [delivery.md](delivery.md).
- topic subscription, wildcards and filters. See [routing.md](routing.md).
- the internal queues, the SQS publish queue, automatic retries and auto-disable.
- the Valkey records and PostgreSQL tables the service writes.

Out of scope:

- listing all tenants (`GET /api/v1/tenants` needs a search module that Valkey does not provide)
- the `/api/v1/metrics/*` endpoints and the portal
- other destination types (webhook, Kafka, RabbitMQ, GCP, Azure, …) and their settings
- ClickHouse, OpenTelemetry, operator-event sinks, log retention
- running more than one server process against the same stores at once

## Stored state is part of the contract

Other tools read the service's stores, so their layout is graded, not just the API:

- **Valkey:** tenant, destination and summary records, migration status records,
  key names (including the `DEPLOYMENT_ID` prefix), hash field names and values.
  Timestamps stored in Valkey are compared as numbers. JSON-valued fields are
  compared by value. Credentials and `delivery_metadata` are stored encrypted
  with `AES_ENCRYPTION_SECRET`: their bytes are not compared, but they must not
  contain the plaintext.
- **PostgreSQL:** the tables the migrations create, with their column names and
  types, and the `events` and `attempts` rows written for deliveries.

## Observing results

- **Destination queues:** use `sqs.receive_message(MessageAttributeNames=["All"])`. Inspect the body and the message attributes.
- **EventBridge destinations:** create a rule on the bus with an SQS target, then read the delivered event envelope from that queue.
- **Valkey:** read the records with a Redis client, e.g. `redis-py` (installed).
- **PostgreSQL:** query the log tables, e.g. with `psycopg` (installed). Log writes are batched and asynchronous, so poll for them.
- **REST responses:** status codes and JSON bodies.

Delivery is asynchronous and at least once. Observe results by polling with a
deadline, and give every scenario its own tenant IDs, queue names and other
resources.

## Comparison rules

The grader compares the program's behaviour as follows:

- **JSON bodies** (API responses, error bodies, JSON stored in Valkey or
  PostgreSQL) are compared by value. Object member order does not matter.
  Numbers are compared by numeric value, so `1`, `1.0` and `1e0` are equal.
  Booleans are never equal to numbers. In API resource objects, a member that
  is absent is equivalent to the same member set to `null`. This applies only at
  the top level of a resource, not inside `data`, `metadata`, `config` or `filter`.
- **Error responses** are compared in full: status code, `status`, `message`
  and, when present, `data`.
- **ID lists** are compared without regard to order. This covers the
  `destination_ids` of a publish response and `matched_destination_ids`.
  Lists of events or attempts are ordered by time, newest first unless
  `dir=asc`. The order of records with equal times is not compared.
  Following `next`/`prev` cursors is graded only for the default
  (descending) order.
- **Timestamps** are RFC 3339 instants compared at millisecond precision.
  Server-chosen times are checked against the wall-clock window of the
  operation.
- **Destination message bodies** (the SQS message body and the event data stored
  in PostgreSQL text columns) are compared as exact text. The `metadata` message
  attribute and the EventBridge envelope are JSON and are compared by value.
- **Generated IDs** (event IDs when `id` is omitted, destination IDs) are compared
  only for uniqueness and format: event IDs are UUID strings.
- **Command output** is compared line by line, with runs of whitespace collapsed
  and blank lines ignored. JSON log lines are not compared; named diagnostics,
  such as the refusal of `serve`, are matched as substrings of stderr.
- **Timing:** deliveries and log rows are expected within seconds. Retry delays
  are checked against the configured schedule with a tolerance of a few seconds.
  After a restart, one internal-queue visibility timeout (30 s) of delay is tolerated.
