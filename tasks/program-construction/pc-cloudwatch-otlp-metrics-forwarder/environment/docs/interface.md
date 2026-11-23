# otlp-emf-forwarder: interface

`otlp-emf-forwarder` is a long-running agent. It receives OpenTelemetry (OTLP)
metrics and forwards them to Amazon CloudWatch Logs as Embedded Metric Format
(EMF) JSON log events. This guide covers how to invoke it, how to connect it to
the AWS endpoint, and the option surface in scope.

## Invocation

```
program start [OPTIONS]
program --version
program --help
program start --help
```

`start` runs in the foreground until it gets `SIGTERM` or `SIGINT`, then
exits with status 0. An invalid option value is rejected with a nonzero exit
status and a diagnostic on stderr. It logs to stdout. It does not need a
writable working directory and stores no local state. Only the metrics
pipeline is in scope; traces and logs are not.

A typical invocation against the task's AWS endpoint:

```bash
program start \
  --otlp-grpc-endpoint 127.0.0.1:4317 \
  --otlp-http-endpoint 127.0.0.1:4318 \
  --exporter awsemf \
  --awsemf-exporter-custom-endpoint "$AWS_ENDPOINT_URL" \
  --awsemf-exporter-region us-east-1 \
  --awsemf-exporter-log-group-name /my/metrics \
  --awsemf-exporter-log-stream-name my-stream
```

## AWS endpoint and credentials

* `--awsemf-exporter-custom-endpoint URL` sends all CloudWatch Logs API calls
  to `URL`. In the task environment this is `$AWS_ENDPOINT_URL`
  (`http://engines:4566`). Without it the public AWS endpoint for the region
  is used, and that endpoint is not reachable here.
* `--awsemf-exporter-region` takes a region code such as `us-east-1`. It is
  used for request signing and for the default endpoint.
* Credentials come from the standard AWS environment variables
  `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` and the optional
  `AWS_SESSION_TOKEN`. A region must also be resolvable from `AWS_REGION` or
  `AWS_DEFAULT_REGION` when the program starts. The task environment sets
  these.

The program uses only the CloudWatch Logs JSON API (`Logs_20140328.*`
operations) over HTTP.

## Receiving metrics

| Option                 | Default          | Meaning                                    |
|------------------------|------------------|--------------------------------------------|
| `--otlp-grpc-endpoint` | `localhost:4317` | OTLP/gRPC listen address (`HOST:PORT`)     |
| `--otlp-http-endpoint` | `localhost:4318` | OTLP/HTTP listen address (`HOST:PORT`)     |

* gRPC: `opentelemetry.proto.collector.metrics.v1.MetricsService/Export`.
* HTTP: `POST /v1/metrics` with `Content-Type: application/x-protobuf`
  (binary `ExportMetricsServiceRequest`) or `application/json`. A successful
  request returns HTTP 200.
* With the OTLP JSON encoding, field names are lowerCamelCase, timestamps
  (`timeUnixNano`, `startTimeUnixNano`) are decimal strings, enum fields such
  as `aggregationTemporality` are integers, and other 64-bit integers
  (`asInt`, `intValue`, `count`, `bucketCounts`) are JSON numbers. Requests
  are sent in that form.

The metric data types in scope are gauge, sum (cumulative and delta
temporality, monotonic or not), histogram (either temporality) and summary.
Exponential histograms are out of scope. Attribute values may be strings,
booleans, integers, doubles, arrays or bytes. A sender controls each data
point's `timeUnixNano`; requests use timestamps within a few seconds of the
current wall-clock time, and summary counts never decrease within a series.
How a request containing an unusable data point (for example one without a
value) is handled is part of the behavior to observe.

Received data is batched before it is exported, with a default batch timeout
of 200 ms. Requests that arrive within one batch window can be processed
together. Export is asynchronous: the HTTP/gRPC response does not mean the
data is in CloudWatch Logs yet. A failed CloudWatch call is retried with
backoff.

## Exporter options in scope

Every option can also be set through an environment variable: add the prefix
`ROTEL_`, use upper case, and replace hyphens with underscores. For example,
`ROTEL_AWSEMF_EXPORTER_NAMESPACE`.

| Option                                                    | Default            |
|-----------------------------------------------------------|--------------------|
| `--exporter awsemf`                                       | (`otlp`)           |
| `--awsemf-exporter-region`                                | `$AWS_REGION`, else `$AWS_DEFAULT_REGION`, else `us-east-1` |
| `--awsemf-exporter-custom-endpoint`                       | (unset)            |
| `--awsemf-exporter-log-group-name`                        | `/metrics/default` |
| `--awsemf-exporter-log-stream-name`                       | `otel-stream`      |
| `--awsemf-exporter-log-retention` (days)                  | `0` (none)         |
| `--awsemf-exporter-namespace`                             | (unset)            |
| `--awsemf-exporter-retain-initial-value-of-delta-metric`  | `false`            |
| `--awsemf-exporter-include-dimensions` (comma-separated)  | (all)              |
| `--awsemf-exporter-exclude-dimensions` (comma-separated)  | (none)             |

* Log group and stream: the program writes every EMF document as one log
  event into the configured group and stream. If they do not exist, it
  creates them. A non-zero retention (one of the CloudWatch Logs retention
  values, in days) is applied to a log group that the program creates.
* Namespace: `--awsemf-exporter-namespace` sets the CloudWatch metric
  namespace explicitly. If it is unset, the namespace comes from the
  OpenTelemetry resource (the `service.namespace` and `service.name`
  attributes) and falls back to a default.
* Cumulative data (cumulative sums, summaries) is reported to CloudWatch as
  the change between consecutive exports of the same series. The
  `retain-initial-value-of-delta-metric` flag controls whether the first
  observed value of a series is reported.
* Dimension filtering: by default, every resource attribute and data point
  attribute becomes a CloudWatch dimension. `include-dimensions` and
  `exclude-dimensions` take wildcard patterns, where `*` matches zero or more
  characters, for example `service.*,http.*` or `*.internal`. A dimension must
  match an include pattern, when any are given, and must match no exclude
  pattern. Filtering affects only the dimension declarations of the metric
  definition. Attribute values are still written into the log event.

Retry tuning (`--awsemf-exporter-retry-*`), batching flags, processors and
other exporters and receivers are outside the task scope.

## Observing results

Results are read back through the CloudWatch Logs API, for example with boto3
`get_log_events` or `filter_log_events` on the configured group and stream,
`describe_log_streams`, and `describe_log_groups` for retention. Each event's
`message` is one complete EMF JSON document, as described in `emf-format.md`.
Its event `timestamp` is the document's `_aws.Timestamp`.

Results are compared with the reference program's under these rules:

* The set of documents written must match, with multiplicity. The order of log
  events is not significant.
* Each document is compared completely: no member may be missing or extra,
  including in metric definitions.
* Key order inside JSON objects is not significant. The order of entries in
  `Metrics` and of names within a dimension set is not significant.
* Numbers compare by value, so `15`, `15.0` and `1.5e1` are equal. Booleans,
  strings and numbers are distinct types; for example `"5"` is not `5`.
* Exports meant to be distinct are sent at least one second apart. Results
  are awaited for up to about 30 seconds, including when the log group or
  stream must first be created. A few seconds of extra waiting follow, to make
  sure nothing else is written.
* Log groups and streams are otherwise unchanged: existing events and other
  streams stay as they were.
