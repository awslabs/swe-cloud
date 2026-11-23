# trace-index-lifecycle — public interface

`trace-index-lifecycle` is a single Linux x86-64 executable (called `program` below; the
reference is `/reference/program`, yours must be `/app/program`) with two faces:

1. **Index lifecycle tool** — `program es-rollover <action> URL [flags]` manages index
   templates, numbered indices and read/write aliases in an OpenSearch cluster.
2. **Trace server** — `program --config file:PATH` runs a long-lived process that accepts
   OpenTelemetry (OTLP) spans, stores them in OpenSearch through the write aliases, and
   serves a JSON query API that reads through the read aliases.

Everything else the executable may print or offer (UI pages, sampling, metrics, archive
storage, other storage backends, index cleaning, ILM/ISM-managed rollover, data streams,
dependency links, feature gates, TLS and authentication) is outside the admitted scope.

## OpenSearch through the AWS venue

The only backing store is an OpenSearch domain obtained from the AWS API
(`AWS_ENDPOINT_URL`, region `us-east-1`, dummy credentials are preset). Creating a domain
starts a real single-node OpenSearch 2.x engine, which can take a minute or two:

```python
import boto3, time
c = boto3.client("opensearch")
c.create_domain(DomainName="my-domain")
while True:
    s = c.describe_domain(DomainName="my-domain")["DomainStatus"]
    if s.get("Endpoint") and not s.get("Processing"):
        break
    time.sleep(2)
url = s["Endpoint"] if s["Endpoint"].startswith("http") else "http://" + s["Endpoint"]
# ... later: c.delete_domain(DomainName="my-domain")
```

`url` (for example `http://10.89.0.2:9200`) is the value passed to `es-rollover` and to
`server_urls`. Inspect cluster state with the OpenSearch REST API (`GET /_cat/aliases`,
`GET /<prefix>-jaeger-*`, `GET /_template/<name>`, `GET /<index>/_search`, …); the
`opensearch-py` client and `curl` are installed. Use a unique index prefix per scenario and
delete what you create. Many prefixes share one cluster during verification.

## Index lifecycle tool

```
program es-rollover init     URL [--index-prefix P] [--shards N] [--replicas N] [flags]
program es-rollover rollover URL [--index-prefix P] [--conditions JSON]
program es-rollover lookback URL [--index-prefix P] [--unit UNIT] [--unit-count N]
```

* `URL` is exactly one positional argument, e.g. `http://host:9200`.
* Common flags (any action): `--index-prefix`, `--skip-dependencies`, `--adaptive-sampling`,
  `--timeout` (seconds). Every flag can also be given as an environment variable:
  upper-case, with `.` and `-` replaced by `_` (for example `INDEX_PREFIX`, `UNIT_COUNT`,
  `SKIP_DEPENDENCIES=true`). A flag given on the command line wins over the environment.
* `init` flags: `--shards` (default 5), `--replicas` (default 1),
  `--span-numeric-attributes`, `--span-total-fields-limit N`,
  `--priority-{span,service,dependencies,sampling}-template N`.
* `rollover` flag: `--conditions`, an OpenSearch rollover-conditions JSON object
  (default `{"max_age": "2d"}`), e.g. `{"max_docs": 1}`.
* `lookback` flags: `--unit`, one of `seconds, minutes, hours, days, weeks, months, years`
  (default `days`), and `--unit-count` (default 1).
* `--es.use-ilm`, `--archive`, TLS, token/API-key and basic-auth flags are out of scope.
* `program es-rollover --help` and `program es-rollover <action> --help` print usage
  naming the actions and their flags.
* Exit status: 0 on success, non-zero on failure. Logs are JSON lines; their text is
  diagnostic only.

Index families are named `<prefix>-jaeger-<kind>` (`jaeger-<kind>` when the prefix is
empty), where `<kind>` is `span`, `service`, `dependencies` and, with
`--adaptive-sampling`, `sampling`. Concrete indices carry a six-digit generation suffix
(`…-000001`); each family has a `…-read` and a `…-write` alias, and a legacy index
template (`_template` API). Which indices, aliases and templates each action creates,
moves, replaces or leaves alone, how they behave when a step fails partway, and how
actions and the server interact is the behaviour to discover.

## Trace server

Configuration is a collector-style YAML file. This is the admitted configuration; only
the URL, the prefix (with or without a trailing `-`), the per-family `shards`/`replicas`
and the listen ports vary:

```yaml
service:
  extensions: [jaeger_storage, jaeger_query, healthcheckv2]
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [jaeger_storage_exporter]
  telemetry:
    metrics:
      level: none
extensions:
  healthcheckv2:
    use_v2: true
    http:
      endpoint: 127.0.0.1:13133
  jaeger_query:
    storage:
      traces: some_storage
    http:
      endpoint: 127.0.0.1:16686
    grpc:
      endpoint: 127.0.0.1:16685
  jaeger_storage:
    backends:
      some_storage:
        opensearch:
          server_urls: ["http://host:9200"]
          indices:
            index_prefix: "myprefix"
            spans:        {shards: 1, replicas: 0, rotation: {manual_rollover: {}}}
            services:     {shards: 1, replicas: 0, rotation: {manual_rollover: {}}}
            dependencies: {shards: 1, replicas: 0, rotation: {manual_rollover: {}}}
            sampling:     {shards: 1, replicas: 0, rotation: {manual_rollover: {}}}
receivers:
  otlp:
    protocols:
      grpc: {endpoint: 127.0.0.1:4317}
      http: {endpoint: 127.0.0.1:4318}
processors:
  batch:
exporters:
  jaeger_storage_exporter:
    trace_storage: some_storage
```

Run it in the background: `program --config file:/path/config.yaml`; it is stopped with
SIGTERM. The query API answering `GET /api/v3/services` means it is ready. Other
configuration keys (tags-as-fields, custom alias names, other rotation modes, index
creation switches, archive/sampling storage) are out of scope. With `manual_rollover` the
server expects the indices and aliases created by `es-rollover init` with the same prefix;
what it does to the cluster itself at start-up and when they are missing is observable.
Writes are asynchronous (batching plus index refresh): poll the query API or refresh and
poll the cluster until data appears.

The server also exports diagnostic spans about itself over OTLP gRPC to `localhost:4317`
(standard `OTEL_EXPORTER_OTLP_*` variables apply); when nothing listens there it only logs
export errors. Graded scenarios never listen on that address.

### Intake

OTLP/HTTP: `POST /v1/traces` on the `otlp` HTTP endpoint with
`Content-Type: application/json` (OTLP JSON encoding: hex `traceId`/`spanId`, decimal
string nanosecond timestamps, integer enums). The OTLP JSON mapping is defined by the
public OpenTelemetry protocol specification (`opentelemetry/proto/trace/v1/trace.proto`
and the OTLP/HTTP JSON encoding rules). Protobuf bodies and OTLP/gRPC are also accepted
by the reference but are not graded. Example body:

```json
{"resourceSpans":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"shop"}}]},
  "scopeSpans":[{"scope":{"name":"lib","version":"1.0"},"spans":[{
    "traceId":"0123456789abcdef0123456789abcdef","spanId":"0123456789abcdef",
    "name":"GET /cart","kind":2,
    "startTimeUnixNano":"1700000000000000000","endTimeUnixNano":"1700000000005000000",
    "attributes":[{"key":"http.method","value":{"stringValue":"GET"}},
                  {"key":"retries","value":{"intValue":"3"}}],
    "events":[{"timeUnixNano":"1700000000001000000","name":"cache miss"}],
    "status":{"code":2,"message":"boom"}}]}]}]}
```

Graded inputs use every OTLP attribute value type (string, bool, int, double, bytes,
array, key/value list), resources with and without `service.name`, scopes with and without
name/version, all span kinds and status codes, trace state, trace flags, events, links
(with and without an `opentracing.ref_type` attribute), zero-length spans and resending
identical spans. They avoid: duplicate span IDs within a trace, child spans outside their
parent's time range, IP-address attributes, and span attributes whose keys collide with
names the server derives itself (`span.kind`, `error`, `otel.status_code`,
`otel.status_description`, `otel.scope.*`, `otel.library.*`, `w3c.tracestate`).

### Query API (on `jaeger_query.http.endpoint`)

| Method/path | Parameters |
|---|---|
| `GET /api/v3/services` | — |
| `GET /api/v3/operations` | `service`, optional `span_kind` |
| `GET /api/v3/traces` | `query.service_name`, `query.operation_name`, `query.start_time_min`, `query.start_time_max` (RFC 3339), `query.duration_min`/`query.duration_max` (Go durations such as `5ms`, `100us`), `query.attributes` (JSON object of string values), `query.search_depth` |
| `GET /api/v3/traces/{trace_id}` | optional `raw_traces` |
| `GET /api/traces/{trace_id}` | legacy JSON trace model (`data[].spans`, `processes`) |

Parameter names are shown in their snake_case form; the camelCase spellings
(`query.serviceName`, `query.startTimeMin`, `spanKind`, …) are accepted too.
`/api/v3/trace-summaries`, `/api/v3/spans`, the `start_time`/`end_time` lookup parameters
and `query.raw_traces` exist but are not graded.

APIv3 trace responses are OTLP JSON wrapped as `{"result": {"resourceSpans": …}}`. Search
and lookup failures are reported with HTTP status codes and a JSON error body
(`{"error": {"httpCode": …, "message": …}}` on APIv3; legacy responses always carry
`data`, `total`, `limit`, `offset` and `errors`). The legacy `/api/services` and legacy trace search routes are not served by this
build.

## What is observed, and what counts as equal

* **es-rollover:** zero versus non-zero exit status, and the resulting cluster state.
  An index's age is its creation date as the cluster reports it in the index metadata
  (`index.creation_date` in its settings). To grade long lookback periods, verification
  may reach the cluster through a transparent HTTP pass-through that reports some
  generations with older creation dates; all requests and their effects still go to the
  real cluster.
  Help is checked only for naming the actions and flags.
* **Cluster state:** the concrete `<prefix>-jaeger-*` indices and which aliases each one
  carries (alias bodies are compared too, e.g. write-index flags); index settings
  (`number_of_shards`, `number_of_replicas`, field limits); index mappings; legacy and
  composable templates under the prefix. Templates are compared as `GET _template`
  reports them: `index_patterns`, `order`, `aliases` and `settings` exactly, `mappings`
  as OpenSearch interprets them (a boolean or number leaf may be spelled as a string).
  Index UUIDs show whether an index was recreated.
* **Span documents** (`_source` of span indices): compared field by field. Order inside
  `tags`, `process.tags`, `logs`, log `fields` and `references` is not significant; an
  absent field, `null` and an empty list or object are equivalent for those lists and for
  the `tag`/`process.tag` objects; absent `parentSpanID` equals `""` and absent `flags`
  equals `0`. Numbers compare by value (`2` equals `2.0`); booleans must be JSON booleans;
  `type` strings compare exactly. Fields outside the stored model are not allowed.
  Document `_id`s are not compared, but which index a document lands in is.
* **Service documents:** exactly `serviceName` and `operationName`; how many exist and in
  which generation is compared.
* **APIv3 traces:** spans are compared as an unordered set after flattening
  `resourceSpans`/`scopeSpans`; each span is compared together with its resource
  attributes and scope name/version. How spans are grouped into resource/scope blocks and
  the order of spans, attributes, events and links are not significant. Fields equal to
  their proto3 default may be omitted (`kind` 0, empty `status`, empty strings, `flags`
  0, empty lists). int64 fields (`intValue`, `…UnixNano`, `flags`) may be decimal strings
  or JSON integers; `doubleValue` compares by value; `bytesValue` is base64; enums are
  integers.
* **Legacy traces:** spans are matched by `spanID`, each with its process resolved
  through `processID` (process keys such as `p1` are arbitrary); order of spans, tags,
  logs and references is not significant; `warnings` are not compared. Tag values follow
  the number/boolean rules above.
* **Services and operations:** list order is not significant.
* **Search:** the set of traces returned, and that each returned trace is complete.
  Order is not significant.
* **Errors:** the HTTP status and the JSON error structure; message text only for
  "not found" bodies.
