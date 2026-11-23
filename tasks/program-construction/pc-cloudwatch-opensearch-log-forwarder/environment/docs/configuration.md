# cloudlog-search-forwarder — configuration reference

Durations use Go syntax (`500ms`, `2s`, `1m`, `1h`). Timestamps use RFC 3339
(`2024-01-02T15:04:05Z`).

## `receivers.aws_cloudwatch`

| Key | Notes |
| --- | --- |
| `region` | Required. AWS region string. |
| `storage` | Optional. ID of a storage extension (e.g. `file_storage`) used to persist the read position per log group. |
| `logs.poll_interval` | Time between polls; default `1m`; must be at least one second. |
| `logs.start_from` | RFC 3339 timestamp from which to start reading. Default: from the beginning. Mutually exclusive with `initial_lookback`. |
| `logs.initial_lookback` | Positive duration back from startup at which to start reading. |
| `logs.max_events_per_request` | Page size requested from CloudWatch Logs; default 1000; must be > 0. |
| `logs.groups.named` | Map of log group name → stream filter (`{}`, `names: [...]`, `prefixes: [...]`). |
| `logs.groups.autodiscover` | Discover groups instead of naming them: `prefix` or `pattern` (substring; not both), `limit` (default 50, > 0), `streams.names`, `streams.prefixes`. |

`named` and `autodiscover` are mutually exclusive. When neither is given the receiver
discovers groups with the autodiscover defaults.

## `processors.batch`

Standard batching (`timeout`, `send_batch_size`, `send_batch_max_size`). Optional.

## `exporters.opensearch`

| Key | Notes |
| --- | --- |
| `http.endpoint` | Required. OpenSearch base URL. Other HTTP client settings (`timeout`, `headers`, `compression`, `tls`) are accepted. |
| `dataset` | Default `default`. Used in the default index name and recorded in documents. |
| `namespace` | Default `namespace`. Used in the default index name and recorded in documents. |
| `logs_index` | Optional index-name pattern. `%{key}` placeholders are looked up in record attributes, then scope attributes, then resource attributes. `scope.name` and `scope.version` are aliases for the instrumentation scope's name and version, distinct from scope attributes. |
| `logs_index_fallback` | Replacement for an unresolved placeholder (default `unknown`). |
| `logs_index_time_format` | Optional UTC time suffix (tokens `yyyy yy MM dd HH mm ss`; separators `- . _ +`), appended as `-<suffix>`. |
| `bulk_action` | `create` (default) or `index`. |
| `pipeline` | Optional OpenSearch ingest pipeline name. |
| `retry_on_failure` | `enabled`, `initial_interval`, `max_interval`, `max_elapsed_time`. |
| `timeout`, `sending_queue` | Standard exporter timeout and queue settings. |
| `mapping.mode` | `ss4o` (default) is the admitted mode. |

## `extensions.file_storage`

| Key | Notes |
| --- | --- |
| `directory` | Directory holding the persisted state. Must exist unless `create_directory: true`. |
| `timeout` | Lock-acquisition timeout. |

The state files are internal to the program; reuse the same directory to resume, or
a fresh one to start over.
