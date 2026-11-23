# event-destination-hub — destinations and delivery

## Destination types

### `aws_sqs`

| Field | Meaning |
|-------|---------|
| `config.queue_url` (required) | target queue URL |
| `config.endpoint` (optional) | SQS API endpoint; set it to `$AWS_ENDPOINT_URL` in the venue |
| `credentials.key`, `credentials.secret` (required); `credentials.session` (optional) | AWS credentials |

Delivered messages carry the event's `data` as the body. Event metadata is
delivered as SQS message attributes. Read them with `MessageAttributeNames=["All"]`.

### `aws_eventbridge`

| Field | Meaning |
|-------|---------|
| `config.event_bus_name` | target bus |
| `config.region` | region |
| `config.endpoint` | EventBridge API endpoint (`$AWS_ENDPOINT_URL`) |
| `credentials.key`, `credentials.secret` (required); `credentials.session` (optional) | AWS credentials |

Each event becomes one `PutEvents` entry on the bus. To observe it, add a rule
with an SQS target to the bus and read the event envelope that the rule delivers.

## Internal pipeline

Published events go to an internal SQS delivery queue. Delivery outcomes go to
an internal SQS log queue, which is persisted to PostgreSQL in batches.
With `MQS_AUTO_PROVISION=true` the server creates these queues and their
dead-letter queues at startup under the configured names.

## Publish queue

With `PUBLISH_AWS_SQS_*` set, the server also consumes the publish queue.
Each message body is a JSON document with the same shape as the `POST /publish`
body. `PUBLISH_MAX_REDELIVERIES` limits how often a failing message is redelivered.

## Failures, retries and disabling

A failed delivery attempt is recorded with status `failed`. Events published
with `eligible_for_retry` (the default) are retried automatically according to
`RETRY_SCHEDULE` or `RETRY_INTERVAL_SECONDS`/`MAX_RETRY_LIMIT`. A retry delay
is a minimum, and delays shorter than `LOG_BATCH_THRESHOLD_SECONDS` are raised
to it. `POST /retry` triggers a manual retry.

When `ALERT_CONSECUTIVE_FAILURE_COUNT` consecutive failures are reached and
`ALERT_AUTO_DISABLE_DESTINATION=true`, the destination is disabled. Its
`disabled_at` is then set, and it receives no further events until it is
re-enabled. Disabling and enabling are also available through the API.
