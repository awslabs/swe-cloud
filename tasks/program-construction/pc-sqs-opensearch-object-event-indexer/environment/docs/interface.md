# bucket-event-indexer: interface

`bucket-event-indexer` is a long-running log shipper. It polls an SQS queue for S3
object notifications, downloads each announced object, splits it into events
(normally one per line), and indexes those events into OpenSearch through the bulk API.
A notification message is removed from the queue only after its events are
handled. The program also has a configuration-validation command.

This guide covers syntax, scope and how results are compared. It does not give the
exact consequences of each option. Establish those by running `/reference/program`:
which documents appear, with which fields, ids and index names, which messages remain
queued, what is retried, and the exit statuses.

## Invocation

```
program --config PATH          # run the pipeline (also: -c PATH); runs until SIGTERM/SIGINT
program validate [--no-environment] [--deny-warnings] PATH...
```

* The configuration format is chosen by file extension: `.toml`, `.yaml`/`.yml` or
  `.json`. All examples here are TOML.
* `validate` checks the configuration. Without `--no-environment` it also builds
  the components and runs their health checks against the configured endpoints.
  `--deny-warnings` turns warnings into failures. The exit status is `0` for a
  valid configuration and `78` for one that fails validation. A running pipeline that
  cannot load its configuration also exits `78`.
* A running pipeline stops gracefully on SIGTERM or SIGINT.
* While running, the program writes its own diagnostics to stdout/stderr. These logs
  are not part of the graded interface.
* `data_dir` (top level) names a writable directory for local state.
* Configuration values are used literally. Environment-variable interpolation is not
  part of this task.

## AWS and OpenSearch setup

All AWS calls go to the emulator at `http://engines:4566`, which is also exported as
`AWS_ENDPOINT_URL`. The emulator uses region `us-east-1` and dummy credentials, which
the program takes from the standard `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`
environment variables.

OpenSearch is provisioned through the AWS OpenSearch API:
`opensearch.create_domain(DomainName=..., EngineVersion="OpenSearch_2.11")`. Poll
`describe_domain` until `Endpoint` is present and `Processing` is false. The endpoint
is a `host:port` reachable over plain HTTP, so add `http://` if needed and wait until
`GET /` answers.

The configured `endpoints` URL is not always OpenSearch itself. It may be a plain HTTP
intermediary that forwards to OpenSearch, records each bulk request and can return
error responses: whole-request HTTP errors, or HTTP 200 bulk responses whose items
carry their own failure statuses, in the same compact JSON format OpenSearch uses.
Talk to OpenSearch only through the configured endpoints and the standard bulk API
(`POST <endpoint>/_bulk` with newline-delimited action/document pairs).

S3 notifications have the standard S3 event message structure
(`{"Records":[{"eventVersion":"2.1","eventSource":"aws:s3","awsRegion":...,
"eventTime":...,"eventName":"ObjectCreated:Put","s3":{"bucket":{"name":...},
"object":{"key":...}}}]}`). They are sent to the queue directly with SQS
`SendMessage`. Do not rely on the emulator to generate them. Object keys in
notifications are URL-encoded, as S3 encodes them. A notification may also arrive
wrapped in an SNS envelope (`{"Type":"Notification","Message":"<s3 event json>",
"Timestamp":...}`). The S3 `s3:TestEvent` message may also appear. A message may
contain several records, and some messages are not valid notifications at all.

## Configuration in scope

Exactly one pipeline is admitted: an `aws_s3` source feeding an `elasticsearch`
sink. Component names (`bucket` and `search` below) are arbitrary.

```toml
data_dir = "/work/state"

[sources.bucket]
type = "aws_s3"
region = "us-east-1"
endpoint = "http://engines:4566"   # used for both S3 and SQS
compression = "auto"               # auto | none | gzip | zstd
# decoding.codec = "json"          # bytes (default) | json
# framing.method = "newline_delimited"

[sources.bucket.sqs]
queue_url = "http://engines:4566/000000000000/my-queue"
poll_secs = 1
visibility_timeout_secs = 30
# delete_message = true
# delete_failed_message = true

[sinks.search]
type = "elasticsearch"
inputs = ["bucket"]
endpoints = ["http://<opensearch endpoint>"]
api_version = "v7"                 # required for OpenSearch 2.x
mode = "bulk"                      # bulk | data_stream
bulk.index = "app-%Y.%m.%d"        # template: strftime and {{ field }} references
bulk.action = "index"              # index | create (template allowed)
id_key = "id"                      # event field used as the document _id
request_retry_partial = false
batch.timeout_secs = 1
acknowledgements.enabled = true    # end-to-end delivery acknowledgement
```

### Source options (`type = "aws_s3"`)

| option | meaning |
| --- | --- |
| `region`, `endpoint` | AWS region, and the base URL used for S3 and SQS |
| `force_path_style` | path-style S3 addressing (default `true`) |
| `compression` | object decompression: `auto`, `none`, `gzip`, `zstd` |
| `decoding.codec` | `bytes` (each frame becomes `message`) or `json` (each frame is JSON whose fields become event fields) |
| `framing.method` | frame splitting: `newline_delimited` (default) or `character_delimited` with `framing.character_delimited.delimiter` |
| `multiline` | join consecutive lines: `start_pattern`, `condition_pattern`, `mode` (`continue_through`, `continue_past`, `halt_before`, `halt_with`), `timeout_ms` |
| `sqs.queue_url` | queue polled for notifications |
| `sqs.poll_secs` | long-poll wait per receive |
| `sqs.visibility_timeout_secs` | visibility timeout requested when receiving |
| `sqs.delete_message` | delete messages after successful processing (default `true`) |
| `sqs.delete_failed_message` | delete messages whose events were permanently rejected (default `true`) |
| `sqs.max_number_of_messages` | messages per receive, 1–10 |
| `sqs.client_concurrency` | number of concurrent pollers |

Each event has the fields `bucket`, `object` (the decoded key), `region`,
`source_type` and `timestamp`. With the `bytes` codec it also has `message`. With
the `json` codec it has the decoded value's fields instead. The object's user
metadata (`x-amz-meta-*`) also contributes fields. How these sources of fields
interact, and where `timestamp` comes from, is for you to establish.

`compression = "auto"` decides from the object's metadata and key. It does not sniff
the content. Gzip (RFC 1952, possibly several concatenated members) and zstd
(RFC 8878, possibly several frames) are the only compressed formats.

### Sink options (`type = "elasticsearch"`)

| option | meaning |
| --- | --- |
| `endpoints` | list with one OpenSearch base URL |
| `api_version` | `v7` or `v8` against OpenSearch 2.x (`auto` is not supported in this venue) |
| `mode` | `bulk` or `data_stream` |
| `bulk.index` | index name template (`%Y`, `%m`, `%d`, ... and `{{ field }}`, including dotted paths) |
| `bulk.template_fallback_index` | index used when the template cannot be rendered |
| `bulk.action` | `index` or `create`, or a template producing one of them per event |
| `id_key` | event field (dotted paths allowed) used as the document `_id` |
| `request_retry_partial` | retry the failed items of a partially failed bulk request |
| `data_stream.type` / `.dataset` / `.namespace` / `.auto_routing` / `.sync_fields` | data stream naming in `data_stream` mode (index templates with `data_stream` enabled are created by whoever runs the pipeline) |
| `batch.max_events`, `batch.timeout_secs` | batching |
| `request.retry_attempts`, `request.retry_initial_backoff_secs`, `request.retry_max_duration_secs`, `request.timeout_secs` | HTTP retry/timeouts |
| `encoding.only_fields`, `encoding.except_fields` | restrict the indexed fields (dotted paths allowed) |
| `acknowledgements.enabled` | wait for the sink's outcome before the source treats a message as handled |
| `healthcheck.enabled` | health check at startup and in `validate` |

Out of scope:
* other sources, sinks and transforms, including VRL;
* the HTTP API, disk buffers and the deferred queue (`sqs.deferred`);
* request signing or authentication for OpenSearch, TLS, ingest pipelines, custom query
  parameters, external document versioning (`bulk.version*`), the `update`/`delete` bulk
  actions;
* the alternative log namespace, and subcommands other than `validate`.

## Observation and comparison rules

* **Documents.** Each test inspects every document in its own indices after a
  refresh. It looks at `_index`, `_id`, `_source` and `_version`. `_version` shows how
  many times a document was written. Document order is not significant. Generated
  ids (when an event supplies none) are compared only for being distinct and not
  derived from event data.
* **Values.** `_source` is compared as JSON. Strings, booleans, nulls, objects and arrays
  must match exactly. Numbers are compared by value: `1000`, `1000.0` and `1e3` are
  equal, but a number never equals a boolean or a string.
* **Timestamps the program supplies** (the `timestamp` field it adds, or `@timestamp`
  in data-stream mode) must be RFC 3339 UTC strings. They are compared as instants, so
  any precision or `Z`/offset spelling of the same instant is accepted. A timestamp-like
  value that came from the object's own content is an ordinary value and is compared
  exactly.
* **Queue state.** Tests check which messages are deleted, which remain (visible or
  in flight), and how often a remaining message was received (`ApproximateReceiveCount`).
  So retention and redelivery after the visibility timeout are both observable.
* **Bulk traffic through an intermediary.** Tests record the sequence of bulk requests:
  the action, `_index`, `_id` and document of each item, with items in the order sent.
  They do not inspect other request details (URL query, headers, compression of the
  request body).
* The program processes notifications asynchronously. Tests poll with generous
  timeouts (tens of seconds) and stop the program with SIGTERM.
