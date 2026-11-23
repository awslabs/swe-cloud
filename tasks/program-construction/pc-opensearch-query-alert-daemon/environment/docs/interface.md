# query-alert-daemon — public interface

`query-alert-daemon` is a long-running process that runs search queries on a
schedule against an OpenSearch (Elasticsearch-compatible) cluster. It can
suppress results with conditions, summarize them with filters, and send alerts
to a local file or to an Amazon SNS topic. It records each execution in *state
indices* in the same cluster, which a restarted process consults to resume
the schedule.

This guide covers the admitted scope: non-distributed operation, the `file` and
`sns` outputs, and the state indices. Slack/e-mail outputs, distributed
operation (Consul), TLS connections and ILM policies, and rule reloading on
`SIGHUP` are out of scope. Configuration *validation* of these features still
applies at start-up (see below).

## Invocation

```
program            # start the daemon (no positional arguments)
program -version   # print a version banner and exit 0
program -h         # print flag usage
```

The daemon runs until it receives `SIGINT` or `SIGTERM`. It then stops its
schedules and exits with status 0. If configuration loading or validation
fails, it exits with status 1. Log lines go to standard error. They are
informational only, and their wording and format are not part of the interface.

### Environment

| Variable | Meaning | Default |
|---|---|---|
| `GO_ELASTICSEARCH_ALERTS_CONFIG_FILE` | path of the main configuration file | `/etc/go-elasticsearch-alerts/config.json` |
| `GO_ELASTICSEARCH_ALERTS_RULES_DIR` | directory containing rule files (`*.json`) | `/etc/go-elasticsearch-alerts/rules` |
| `GO_ELASTICSEARCH_ALERTS_ES_USERNAME` / `GO_ELASTICSEARCH_ALERTS_ES_PASSWORD` | optional HTTP basic auth sent to the cluster; set both or neither | unset |
| `AWS_ENDPOINT_URL`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | standard AWS SDK settings used by the `sns` output | set by the venue |

A leading `~` in the two paths is expanded to the home directory.

## AWS venue

The AWS emulator answers at `$AWS_ENDPOINT_URL` (region `us-east-1`, dummy
credentials). Use the OpenSearch Service API to create a search cluster. For
example, with boto3:

```python
os_ = boto3.client("opensearch")
os_.create_domain(DomainName="alerts-demo", EngineVersion="OpenSearch_2.11",
                  ClusterConfig={"InstanceType": "t3.small.search", "InstanceCount": 1})
# poll describe_domain(...)["DomainStatus"] until "Endpoint" is set and "Processing" is False
```

The domain serves plain HTTP with no authentication. Put `http://<Endpoint>` in
`elasticsearch.server.url`, adding the `http://` scheme if the returned value
has none. Clean up with `delete_domain`.

The `sns` output publishes through the standard AWS SDK credential/endpoint
chain, so it honours `AWS_ENDPOINT_URL`. To observe published messages,
subscribe an SQS queue to the topic (`Protocol="sqs"`) and read the SNS
notification envelope. The alert text is in its `Message` field.

## Main configuration file (JSON)

```json
{
  "elasticsearch": {
    "server": { "url": "http://search-host:9200" },
    "client": { "tls_enabled": false }
  }
}
```

- `elasticsearch.server.url` (required): base URL of the cluster. A trailing `/` is ignored.
- `elasticsearch.client` (optional): leave TLS disabled in this venue. Enabling
  `tls_enabled` requires `ca_cert`, `client_cert` and `client_key` paths.
- `elasticsearch.state_template.ilm_policy_name`: out of scope, since OpenSearch has no ILM.
- `distributed`: omit it or set it to `false`. When it is `true` a `consul`
  object is required.
- Unknown fields are ignored.

Invalid configuration or rules are reported before anything is scheduled: the
process exits with status 1 without querying, alerting or writing state.

## Rule files

Every `*.json` file in the rules directory defines one rule. At least one rule
is required.

```json
{
  "name": "Checkout Errors",
  "index": "app-logs-*",
  "schedule": "@every 30s",
  "body": { "query": { "term": { "level": "error" } },
            "aggs": { "service": { "terms": { "field": "service" } } } },
  "body_field": "hits.hits._source",
  "filters": ["aggregations.service.buckets"],
  "conditions": [ { "field": "hits.hits._source.latency", "quantifier": "any", "gt": 250 } ],
  "outputs": [
    { "type": "file", "config": { "file": "/work/alerts.jsonl" } },
    { "type": "sns",  "config": { "region": "us-east-1",
                                  "topic_arn": "arn:aws:sns:us-east-1:000000000000:alerts",
                                  "template": "{{range .}}{{.Filter}} {{end}}" } }
  ]
}
```

| Field | Type | Notes |
|---|---|---|
| `name` | string, required | rule name, used in alerts and state documents |
| `index` | string, required | index or pattern searched (`<index>/_search`) |
| `schedule` | string, required | schedule syntax of `github.com/robfig/cron` v1 (see below) |
| `body` | JSON object, or a string holding a JSON object; required | search request body, sent as given |
| `body_field` | string, optional | path whose matched objects become the alert text; default `hits.hits._source` |
| `filters` | array of strings, optional | paths whose matched aggregation buckets are summarized |
| `conditions` | array, optional | all must hold for the rule to alert |
| `outputs` | array, required (≥1) | each item has `type` and a non-empty `config` object |

### Schedule

The schedule uses robfig/cron v1 syntax. This is either a six-field cron
expression with a leading *seconds* field (`*/10 * * * * *`), a descriptor
(`@hourly`, `@daily`, …), or `@every <Go duration>` (`@every 5s`, `@every 2m`).
Times are computed by the process clock.

### Paths

`body_field`, `filters` and condition `field` values are dot-separated paths
into the JSON search response (for example
`aggregations.by_host.buckets.by_program.buckets`). A path steps through JSON
arrays automatically. There is no index or wildcard syntax. Run the rule's
query yourself to see the response shape before writing paths.

- A `filters` path is expected to reach aggregation bucket objects (`key`, `doc_count`).
- A `body_field` path is expected to reach JSON objects, such as each hit's `_source`.

### Conditions

Each condition has:

- `field` (required): the path to primitive values.
- `quantifier`: `any` (default), `all` or `none`.
- Comparison operators: `eq` and `ne` take a string or a number. `lt`, `le`, `gt` and `ge` take a number.

A rule alerts only when every condition holds.

### Outputs

- **file**: `config.file` is a path. Each alert appends one JSON object on its
  own line, with `rule_name`, `received_at` and `results` (the alert records).
  The file is created with owner-only permissions (`0600`) if it does not
  exist; its directory must already exist.
- **sns**: `config.region`, `config.topic_arn` and `config.template` are all
  required. The template uses Go `text/template` syntax, with the Sprig
  function library available. Its data (`.`) is the list of alert records for
  one alert. Each record exposes `.Filter` (the path that produced it), `.Text`
  (the stringified `body_field` matches) and `.Fields` (a list with `.Key` and
  `.Count`). The message is published to the topic.

  Graded templates use only this subset: text, `{{range …}}…{{end}}`,
  `{{if …}}…{{end}}`, field access on `.` (`.Filter`, `.Text`,
  `.Fields`, `.Key`, `.Count`), the constants `true`/`false`, string literals,
  parenthesized expressions, pipelines with `|`, the built-ins `len` and
  `index`, and the Sprig functions `upper` and `quote`. A template that does
  not parse is a configuration error; one that fails while rendering (for
  example a field that does not exist) does not publish.

If a query returns nothing to report, no output is written for that execution.
Outputs are independent: a delivery that fails (for example an unwritable path
or an SNS error) is retried a limited number of times and does not prevent the
other outputs of the same alert.

## State indices

At start-up the process tries to install a composable index template named
`go-es-alerts-0.0.3` for its state indices. The OpenSearch version in this
venue rejects that request; the daemon logs the failure and keeps running.
The template request and its contents are not graded. Instead, the test
environment installs its own template before any daemon starts:

```json
{
  "index_patterns": ["go-es-alerts-status-0.0.3-*"],
  "priority": 50,
  "template": {
    "aliases": {"go-es-alerts-0.0.3": {}},
    "mappings": {"properties": {
      "@timestamp": {"type": "date"}, "rule_name": {"type": "keyword"},
      "next_query": {"type": "date"}, "hostname": {"type": "keyword"},
      "hits_count": {"type": "long"}, "hits": {"type": "object", "enabled": false}}}
  }
}
```

Install the same template on your development domain to reproduce the graded
setting. Whether your program sends its own template request is up to you, but
it must not leave a template that overrides this one.

After each scheduled execution, the process normally writes one state document
for the rule into the daily index `go-es-alerts-status-0.0.3-<yyyy.MM.dd>` (UTC date).
The document contains:

- `@timestamp`: when it was written.
- `rule_name`: a normalized form of the rule name.
- `next_query`: the next scheduled time, RFC 3339.
- `hostname`: the host name of the machine running the daemon.
- `hits_count`
- `hits`

When a process starts, each rule looks up its most recent `next_query` through
the alias `go-es-alerts-0.0.3`. If that time is in the future, the rule waits
for it. If the time is in the past, or the lookup finds nothing or fails, the
rule runs immediately. State documents may have been written by another
process or host.

## Observation and comparison rules

Tests start the daemon with a fresh configuration and rules directory, wait
for its state documents and outputs, then stop it with `SIGTERM`. They compare:

- **Exit status** of start-up failures, flags and signals.
- **File output**: every line is a JSON object with exactly the keys
  `rule_name`, `received_at` and `results`. `results` is compared in order;
  each record has exactly the keys it needs (`filter` with `fields`, or
  `filter` with `text`), and each field exactly `key` and `doc_count`.
  Strings, including `text` (and the way numbers are spelled inside it), are
  compared exactly. When several rules append
  to one file, their lines may be in any order.
- **SNS**: the notification's `Message`, exactly.
- **State documents**, found by exact `rule_name`: exactly the keys listed
  above (`hits` only when non-empty) and their values.
- JSON numbers are compared by numeric value (`1`, `1.0` and `1e0` are equal);
  booleans and `null` are distinct from numbers. Timestamps may use any
  RFC 3339 form and are compared as instants. Schedules are compared at
  whole-second resolution.
- Allow a few seconds for start-up and delivery; tests tolerate that latency
  but not executions at other times than the schedule and state imply.

Admitted rules list each path in `filters` at most once, never list a filter
path that continues through nested buckets (buckets inside other buckets) at
which an earlier filter path ends, and do not point condition fields at
aggregation bucket objects. Seeded documents contain plain
ASCII text without `<`, `>` or `&`, so escaping of other characters is not
graded. Log lines (standard error) are never compared.

Observation notes:

- Refresh the state indices before searching them or before starting a second
  process that must see earlier state (`POST /go-es-alerts-status-0.0.3-*/_refresh`).
- Outputs are delivered asynchronously shortly after a query runs. Allow a few
  seconds before reading the file or the SQS queue.
- `hostname`, `@timestamp`, `received_at` and generated document IDs depend on
  the environment and the clock.
