# cloudlog-search-forwarder — interface

`cloudlog-search-forwarder` is a long-running forwarder that polls log events from
CloudWatch Logs and indexes them as documents into an OpenSearch cluster. It is a
minimal collector-style distribution: a YAML configuration wires one
CloudWatch Logs receiver (`aws_cloudwatch`), an optional `batch` processor, one
`opensearch` exporter and an optional `file_storage` extension that persists the
receiver's read position between runs.

## Invocation

```
program --config file:/path/to/config.yaml      # run until SIGTERM/SIGINT
program validate --config file:/path/to/config.yaml
program components                              # list the compiled components
program --version
```

`--config` may be repeated; `env:VAR` and `yaml:<inline>` sources are also accepted
and are merged in order. `${env:NAME}` inside the YAML expands an environment variable.
The program runs in the foreground, writes its own diagnostics as log lines to
stderr, and stops gracefully on SIGTERM or SIGINT (shutdown flushes pending exports and
closes the storage extension). A configuration error is reported on stderr and the
program exits non-zero without polling.

## AWS and OpenSearch setup

* CloudWatch Logs (and STS, used to obtain the caller's account id) are reached through
  the standard AWS SDK configuration chain: `AWS_ENDPOINT_URL`, `AWS_ACCESS_KEY_ID`,
  `AWS_SECRET_ACCESS_KEY` from the environment. The receiver's `region` key is
  mandatory in the YAML even when `AWS_REGION` is set.
* OpenSearch is addressed only through `exporters.opensearch.http.endpoint`, a URL such
  as `http://host:port`. In this venue an OpenSearch cluster is provisioned through the
  AWS OpenSearch Service API (`CreateDomain`); once the domain is available, its
  `DomainStatus.Endpoint` (prefix it with `http://` when it has no scheme) is the
  value to configure. The cluster accepts unauthenticated HTTP.
* Log events are created with the ordinary CloudWatch Logs API (`CreateLogGroup`,
  `CreateLogStream`, `PutLogEvents`). Polling covers event timestamps up to the time of
  each poll, so events should carry timestamps that are not in the future.

## Minimal configuration

```yaml
extensions:
  file_storage:
    directory: /work/state          # must exist and be writable
receivers:
  aws_cloudwatch:
    region: us-east-1
    storage: file_storage           # optional: persist read position
    logs:
      poll_interval: 5s
      groups:
        named:
          /my/app: {}
processors:
  batch: {}
exporters:
  opensearch:
    http:
      endpoint: http://opensearch-host:9200
    dataset: myapp
    namespace: prod
service:
  extensions: [file_storage]
  telemetry:
    metrics:
      level: none                   # avoids opening an internal metrics port
  pipelines:
    logs:
      receivers: [aws_cloudwatch]
      processors: [batch]
      exporters: [opensearch]
```

See `configuration.md` for the admitted keys.

## Observable results

The observable result is the set of documents in OpenSearch: one document per
forwarded CloudWatch log event, in the index chosen by the exporter's index settings
(by default `ss4o_logs-<dataset>-<namespace>`). Documents follow the Simple Schema for
Observability log shape: a string `body`, an `@timestamp` taken from the event time,
an `observedTimestamp`, record `attributes`, a `resource` object identifying where the
event came from, and an `instrumentationScope`. Document `_id`s are assigned by
OpenSearch and are not part of the interface; `observedTimestamp` is wall-clock and
not reproducible. Document order within an index is not significant; multiplicity
(duplicates or missing events) is.

## Scope

In scope: the logs pipeline only — named log groups or autodiscovery of groups,
stream-name and stream-prefix filters, `poll_interval`, `start_from`/`initial_lookback`,
`max_events_per_request`, persistence of the read position via `file_storage` across
restarts, SS4O log documents, index-name resolution (`dataset`/`namespace`,
`logs_index` placeholders, fallback and time suffix), bulk indexing and retry
settings. Out of scope: metrics and traces pipelines, other mapping modes, IMDS or
profile-based authentication, cross-account options, and every component not listed
by `program components`.

## How results are judged

* **Documents.** Each document's `_source` is read back from OpenSearch and compared as
  a whole JSON object: the same keys at every nesting level, the same JSON types and the
  same string values, with nothing missing and nothing extra. Two fields are compared
  differently: `@timestamp` as an instant (any RFC 3339 spelling of the event's
  millisecond timestamp is equivalent), and `observedTimestamp` only as an RFC 3339
  timestamp taken from the wall clock while the program runs.
* **Placement.** The index holding each document is compared exactly (for a data
  stream, its backing index). Indices the program creates matter too.
* **Order and multiplicity.** Document order is irrelevant; duplicates and missing
  documents count.
* **Runs.** A test starts `program --config file:<path>` (sometimes with further
  `--config` sources and environment variables) in a scratch working directory, uses
  poll intervals of 1–6 s, and allows a few seconds after each poll for documents
  to become searchable. It then sends SIGTERM (occasionally SIGINT) and expects
  exit status 0 within a few seconds. The CloudWatch Logs and OpenSearch state a run
  leaves behind, including state persisted for the next run, is what is judged.
* **Configuration errors.** `validate` exits 0 for a valid configuration. For an
  invalid one, both `validate` and a normal run must exit non-zero, without polling,
  and the stderr diagnostic must name the offending component (`aws_cloudwatch`,
  `opensearch` or `file_storage`). Otherwise the wording is free. Ordinary log lines,
  `components` and `--version` output are not judged.

## Venue notes

The venue's CloudWatch Logs applies `FilterLogEvents` time windows (both ends
inclusive), stream-name lists and `nextToken` paging, and returns events in timestamp
order. It ignores `logStreamNamePrefix`, `logGroupNamePattern` and the
`DescribeLogGroups` page limit. Groups are listed in name order. A request for a group
that does not exist returns no events. Because of this, the filtering effect of stream
`prefixes` and of autodiscover `pattern` is not judged. Their configuration rules still
are.
