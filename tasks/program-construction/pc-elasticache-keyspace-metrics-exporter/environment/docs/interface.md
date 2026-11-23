# keyspace-gauges: interface guide

`keyspace-gauges` is a long-running HTTP service. It connects to a Valkey (Redis
protocol) server and publishes gauges about selected keys, key groups, streams and
user Lua scripts in Prometheus text exposition format. Every HTTP request triggers
a fresh, read-only collection against the server.

## Invocation

```
program [flags]
```

Flags take one leading dash (`-check-keys=...` and `-check-keys ...` both work).
Two leading dashes are also accepted. `program -help` lists every flag. Most flags
can also be set through an environment variable, shown in brackets below. An
explicit flag overrides its variable.

The process logs to stderr and runs until it receives SIGINT or SIGTERM.

### Connection and HTTP

| Flag [env] | Meaning |
|---|---|
| `-redis.addr` [`REDIS_ADDR`] | Server URL used by `/metrics`, e.g. `redis://host:port`. The default is `redis://localhost:6379`. |
| `-web.listen-address` [`REDIS_EXPORTER_WEB_LISTEN_ADDRESS`] | HTTP listen address. The default is `:9121`. |
| `-web.telemetry-path` [`REDIS_EXPORTER_WEB_TELEMETRY_PATH`] | Path of the metrics page. The default is `/metrics`. |
| `-namespace` [`REDIS_EXPORTER_NAMESPACE`] | Prefix of every metric name. The default is `redis`. |
| `-redis-only-metrics` [`REDIS_EXPORTER_REDIS_ONLY_METRICS`] | Leaves out the process metrics of the service itself. |

### Key, stream and script selection (in scope)

| Flag [env] | Meaning |
|---|---|
| `-check-keys` [`REDIS_EXPORTER_CHECK_KEYS`] | Comma-separated glob patterns, found with SCAN. The service exports the size/length of each matching key and, for string keys, its value. |
| `-check-single-keys` [`REDIS_EXPORTER_CHECK_SINGLE_KEYS`] | Comma-separated exact key names, looked up directly (no globbing). |
| `-count-keys` [`REDIS_EXPORTER_COUNT_KEYS`] | Comma-separated glob patterns. The service exports the number of keys that match each one. |
| `-check-key-groups` [`REDIS_EXPORTER_CHECK_KEY_GROUPS`] | Comma-separated [Lua patterns](https://www.lua.org/manual/5.1/manual.html#5.4.1) that classify keys into groups. The patterns are applied in order. A group's name is the concatenation of the capture groups of the first pattern that matches. A key that no pattern matches belongs to `unclassified`. |
| `-max-distinct-key-groups` [`REDIS_EXPORTER_MAX_DISTINCT_KEY_GROUPS`] | Maximum number of distinct key groups reported per database. The remaining groups are combined into `overflow`. The default is 100. |
| `-check-streams` [`REDIS_EXPORTER_CHECK_STREAMS`] | Comma-separated glob patterns for stream keys. The service exports stream, consumer-group and consumer information. |
| `-check-single-streams` [`REDIS_EXPORTER_CHECK_SINGLE_STREAMS`] | Comma-separated exact stream key names. |
| `-streams-exclude-consumer-metrics` [`REDIS_EXPORTER_STREAMS_EXCLUDE_CONSUMER_METRICS`] | Leaves out the per-consumer series. |
| `-check-keys-batch-size` [`REDIS_EXPORTER_CHECK_KEYS_BATCH_SIZE`] | SCAN `COUNT` hint used for scanning. The default is 1000. |
| `-disable-exporting-key-values` [`REDIS_EXPORTER_DISABLE_EXPORTING_KEY_VALUES`] | Stops the export of string values for checked keys. |
| `-script` [`REDIS_EXPORTER_SCRIPT`] | Comma-separated paths to Lua scripts. Each script runs on the server with EVAL on every collection. A script returns a flat array of alternating name and value strings, like an HGETALL reply. |
| `-lua-script-read-only` [`REDIS_EXPORTER_LUA_SCRIPT_READ_ONLY`] | Runs the scripts with EVAL_RO. |

**Key list syntax.** The key, stream and count lists use this syntax:

```
[dbN=]pattern[,[dbN=]pattern...]
```

- `db3=sessions:*` selects database 3. A bare `N=` is accepted as well.
- An item without a `db` part uses database 0.
- Items are separated by commas. Blanks around the database and the pattern are
  trimmed, and items with an empty database or pattern are ignored.
- Each pattern is URL-unescaped, so `%2C` stands for a literal comma.
- Database numbers are plain decimal numbers without leading zeros.

A malformed list, such as a non-numeric database, is a configuration error. At
startup the process exits with a nonzero status. In a `/scrape` request the
response is HTTP 400.

**Key-group list syntax.** The key-group list is parsed as one CSV record.
Surrounding blanks are trimmed and empty entries are ignored. Groups are
evaluated in every logical database the server reports. When a database has more
groups than the limit, memory usage decides which groups keep their own series.
Ties are broken deterministically.

**Scripts.** Script files are read once, when the service starts. A missing file
is a startup error.

## HTTP endpoints

- `GET <telemetry-path>` (default `/metrics`) collects from `-redis.addr` with the
  configured options.
- `GET /scrape?target=<url>` collects from another server. `target` can be a URL
  such as `redis://host:port` or a bare `host:port`. These query parameters override
  the corresponding flags for that one request:
  - `check-keys`
  - `check-single-keys`
  - `check-streams`
  - `check-single-streams`
  - `count-keys`

  The other options come from the flags. A request without `target`, or with an
  invalid key list, answers HTTP 400.
- `GET /health` returns `ok`.
- `GET /` serves a small HTML index page. Unknown paths answer 404, including
  `/metrics` when the telemetry path is changed.

Otherwise a metrics response is HTTP 200, even when the server cannot be reached
or a collection step fails. The `up` gauge reports whether the collection
succeeded (1) or not (0).

## AWS venue setup

The Valkey server is an Amazon ElastiCache replication group served by the AWS
endpoint in `AWS_ENDPOINT_URL` (region `us-east-1`; dummy credentials are already
set). Create a cluster-mode-disabled Valkey group without an auth token, wait
until its status is `available`, and read the endpoint AWS returns:

```python
import boto3
ec = boto3.client("elasticache")
ec.create_replication_group(ReplicationGroupId="kg-demo", ReplicationGroupDescription="demo",
                            Engine="valkey", CacheNodeType="cache.t3.micro", NumCacheClusters=1)
# poll describe_replication_groups until Status == "available"
rg = ec.describe_replication_groups(ReplicationGroupId="kg-demo")["ReplicationGroups"][0]
ep = (rg.get("NodeGroups") or [{}])[0].get("PrimaryEndpoint") or rg.get("ConfigurationEndpoint")
# program -redis.addr redis://{ep['Address']}:{ep['Port']} ...
```

The service connects to that address directly over the Redis protocol. The service
itself never writes data keys, though a user script run with EVAL may. Python `redis`
and `prometheus_client` packages are installed to seed data and to parse the
exposition text.

## Metric families

Every name below starts with the namespace and an underscore (`redis_` by default).
The label names are listed in parentheses.

| Families | Labels |
|---|---|
| `up` | none |
| `key_size`, `key_value` | `db`, `key` |
| `key_value_as_string` | `db`, `key`, `val` |
| `keys_count` | `db`, `key` (the pattern) |
| `key_group_count`, `key_group_memory_usage_bytes` | `db`, `key_group` |
| `number_of_distinct_key_groups` | `db` |
| `stream_length`, `stream_radix_tree_keys`, `stream_radix_tree_nodes`, `stream_groups`, `stream_entries_added_total`, `stream_first_entry_id`, `stream_last_entry_id`, `stream_last_generated_id`, `stream_max_deleted_entry_id` | `db`, `stream` |
| `stream_group_consumers`, `stream_group_messages_pending`, `stream_group_entries_read`, `stream_group_lag`, `stream_group_last_delivered_id` | `db`, `stream`, `group` |
| `stream_group_consumer_messages_pending`, `stream_group_consumer_idle_seconds` | `db`, `stream`, `group`, `consumer` |
| `script_values` | `filename`, `key` |
| `script_result` | `filename` |

The service also exports other families. For example, it reports per-key memory
usage as `key_memory_usage_bytes`.

## Observation rules

- Output uses the Prometheus text exposition format. Compare parsed samples
  (sample name, complete label set, value), not raw text.
  - Sample order, `# HELP`/`# TYPE` lines and number spelling (`1.7e+12` vs
    `1700000000000`) are not significant.
  - Values are compared numerically. `NaN` and `±Inf` are ordinary values.
- For the families in the table above, the samples that refer to the keys,
  streams, groups and scripts under test must match exactly, with no missing or
  extra series.
  - `key_group_memory_usage_bytes` is checked only for presence (one series per
    reported group) and positive values. A group holding much larger values must
    report more memory.
  - `stream_group_consumer_idle_seconds` is a timing value and is not graded.
- These are out of scope and not graded:
  - every other family, including the per-key `key_memory_usage_bytes`, families
    derived from INFO, CONFIG, clients, latency, slowlog or modules, process and Go
    runtime families, build information, scrape errors and any duration or timing
    measurement
  - stream fields that only newer servers report
  - listing the same key twice in one collection
  - log text and the exact wording of error responses
- A database label has the form `db<N>`. The venue's server is from the Valkey 7.2
  line and has 16 logical databases.
