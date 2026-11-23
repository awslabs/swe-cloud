# lag-status-service — public interface

`lag-status-service` is a long-running consumer-lag monitoring service for Kafka
clusters. It polls a cluster's topic metadata and partition end offsets, reads the
consumer-group offset commits that the brokers record in the internal offsets topic,
keeps a short sliding window of commits per group/partition in memory, and evaluates
each group's health on request. Results are served as JSON over HTTP.

In this environment the Kafka cluster is an Amazon MSK cluster provided by the AWS
endpoint described below.

## Invocation

```
program [--config-dir DIR]
```

* `--config-dir DIR` — directory containing the configuration file `burrow.toml`
  (default: the current directory). This is the only command-line option.
* The process runs in the foreground until it receives SIGINT, SIGTERM or SIGQUIT,
  then stops its modules and exits with status 0. A configuration or startup failure
  exits with a non-zero status and a message on stderr.
* Startup messages go to stderr. Structured (JSON-lines) logs go to stdout unless
  `[logging] filename` is set.
* A PID file is created at `[general] pidfile` (default `burrow.pid` relative to the
  working directory) and removed on clean shutdown; startup fails if a live process
  already holds it. Use a writable location.

Individual configuration values may also be overridden through environment variables named
`BURROW_<SECTION>_<KEY>` (upper case, `.` and `-` replaced by `_`), e.g.
`BURROW_GENERAL_PIDFILE`. The prefix itself is `[general] env-var-prefix`.

## AWS / MSK setup

The AWS endpoint is in `AWS_ENDPOINT_URL` (region `us-east-1`, dummy credentials).
The program itself never calls the AWS API; it only speaks the Kafka protocol to the
brokers named in its configuration. To obtain brokers:

1. Create a cluster with the MSK API (`CreateCluster`/`CreateClusterV2`, one broker is
   enough), poll `DescribeCluster` until `State` is `ACTIVE`.
2. `GetBootstrapBrokers` → `BootstrapBrokerString` (comma-separated `host:port`,
   plaintext, no authentication). Use these entries in `servers=[...]`.
3. Use any Kafka client (e.g. `confluent-kafka` for Python, installed here) to create
   topics, produce records and commit consumer-group offsets.
4. Delete the cluster when done.

The broker creates its internal offsets topic lazily on first consumer-group use. The
service needs that topic to exist (with leaders for all partitions) when it starts.
The service learns about a topic only through periodic metadata/offset polling, so
commits for a topic must be made after the service reports that topic's offsets if
you need them to be accounted for.

## Configuration file (`burrow.toml`, TOML)

Only the sections below are in scope. Do not configure `[zookeeper]` or `[notifier.*]`
sections, `kafka_zk` consumers, TLS/SASL/IAM profiles.

```toml
[general]
pidfile = "/work/run/burrow.pid"        # PID file path
access-control-allow-origin = ""        # optional CORS header value for HTTP replies

[logging]
level = "info"                          # debug | info | warn | error | panic | fatal
# filename = "logs/burrow.log"          # optional; default is stdout

[client-profile.<profile>]
client-id = "burrow-lagchecker"
kafka-version = "2.8.0"                 # Kafka protocol version to speak

[cluster.<cluster>]                     # one per Kafka cluster
class-name = "kafka"
servers = [ "host:port", ... ]
client-profile = "<profile>"
offset-refresh = 10                     # seconds between end-offset polls
topic-refresh = 60                      # seconds between topic metadata refreshes
groups-reaper-refresh = 0               # seconds; 0 disables removal of groups no longer known to the brokers (not exercised; keep 0)

[consumer.<name>]                       # reads offset commits for one cluster
class-name = "kafka"
cluster = "<cluster>"
servers = [ "host:port", ... ]
client-profile = "<profile>"
offsets-topic = "__consumer_offsets"
start-latest = false                    # start reading the offsets topic at its end instead of its beginning
backfill-earliest = false               # with start-latest: also backfill from the beginning
group-allowlist = ""                    # regular expression; empty = all groups
group-denylist = ""                     # regular expression; empty = none

[storage.default]
class-name = "inmemory"
intervals = 10                          # commits kept per partition (window size)
min-distance = 0                        # seconds; commits closer together than this are merged
expire-group = 604800                   # seconds; groups whose last commit is older are dropped
workers = 20
queue-depth = 1
group-allowlist = ""
group-denylist = ""

[evaluator.default]
class-name = "caching"
expire-cache = 10                       # seconds an evaluation result is cached
minimum-complete = 0.0                  # minimum window completeness before a partition is judged
allowed-lag = 0                         # lag tolerance used when judging partitions

[httpserver.default]
address = ":8000"                       # listen address, e.g. "127.0.0.1:8000"
timeout = 300
```

Exactly one storage and one evaluator module are used; if none is configured, the
defaults shown above are used. If no HTTP server is configured the service listens on
a random port.

## HTTP API (v3)

All responses except the two `/burrow/admin` routes are JSON objects
(`Content-Type: application/json`) with at least `error` (boolean), `message`
(string) and `request` (`{"url": ..., "host": ...}`). Unknown clusters, topics or
groups give HTTP 404 with `error: true`, except that the two evaluation routes answer
an unknown group with a `NOTFOUND` evaluation object and
`/topic/:topic/consumers` only checks the cluster. Unrouted URLs give HTTP 404
with a short `{"error":true,"message":...,"result":{}}` body.

| Method & path | Result field(s) |
|---|---|
| `GET /burrow/admin` | plain text `GOOD` |
| `GET /burrow/admin/ready` | plain text; 200 `READY` once all modules started, else 503 |
| `GET /v3/kafka` | `clusters`: list of cluster names |
| `GET /v3/kafka/:cluster` | `module`: cluster module configuration |
| `GET /v3/kafka/:cluster/topic` | `topics`: list of topic names |
| `GET /v3/kafka/:cluster/topic/:topic` | `offsets`: end offset per partition (index = partition) |
| `GET /v3/kafka/:cluster/topic/:topic/consumers` | `consumers`: groups consuming the topic |
| `GET /v3/kafka/:cluster/consumer` | `consumers`: list of group names |
| `GET /v3/kafka/:cluster/consumer/:group` | `topics`: map topic → list of partitions, each with the stored commit window `offsets`, `owner`, `client_id`, `current-lag` |
| `GET /v3/kafka/:cluster/consumer/:group/status` | `status`: group evaluation (partitions that are not OK only) |
| `GET /v3/kafka/:cluster/consumer/:group/lag` | `status`: group evaluation (all partitions) |
| `DELETE /v3/kafka/:cluster/consumer/:group` | removes the group from storage |
| `DELETE /v3/kafka/:cluster/consumer/:group/topic/:topic` | removes one topic from a group |
| `GET /v3/config` | `general`, `logging`, `zookeeper`, `httpserver` configuration |
| `GET /v3/config/{storage,evaluator,cluster,consumer,notifier}` | `coordinator` and `modules` (list of module names) |
| `GET /v3/config/{storage,evaluator,consumer}/:name`, `GET /v3/config/cluster/:cluster` | `module`: module configuration |
| `GET /v3/admin/loglevel`, `POST /v3/admin/loglevel` | `level`: current log level / set it with a JSON body `{"level": "<name>"}` |

### Group evaluation object

```
{
  "cluster": str, "group": str,
  "status": "NOTFOUND" | "OK" | "WARN" | "ERR" | "STOP" | "STALL" | "REWIND",
  "complete": number,                 # fraction of the window that is filled
  "partitions": [ PartitionStatus ],
  "partition_count": int,
  "maxlag": PartitionStatus | null,   # partition with the highest current lag
  "totallag": int
}
PartitionStatus = {
  "topic": str, "partition": int, "owner": str, "client_id": str,
  "status": "OK" | "WARN" | "STOP" | "STALL" | "REWIND",
  "start": Offset | null, "end": Offset | null,   # oldest / newest commit in the window
  "current_lag": int, "complete": number
}
Offset = { "offset": int, "timestamp": int (ms, commit time), "observedAt": int (ms), "lag": int | null }
```

Group-level statuses: `OK`, `WARN` (lagging but progressing) and `ERR` (some partition
is stopped, stalled or rewound); `NOTFOUND` for unknown groups. Partition-level
statuses: `OK`, `WARN`, `STOP`, `STALL`, `REWIND`. How commit windows, broker offsets
and time combine into these statuses is the service's behaviour to observe.

## Scope of grading

Graded: the invocation and process behaviour above, every route in the table, the
configuration keys as echoed by the `/v3/config` routes, the effect of `pidfile`,
`access-control-allow-origin`, refresh intervals, `start-latest`/`backfill-earliest`,
allow/deny lists, `intervals`, `min-distance`, `expire-group`, `expire-cache`,
`minimum-complete` and `allowed-lag`, and how the stored commit windows and
group evaluations react to topics, produced records, commits, broker-side group
deletion, topic deletion, restarts and the passage of time.

Fixtures use commit-only groups: offsets are committed explicitly by a client that
never joins/subscribes, with no commit metadata or leader epoch. `owner` and
`client_id` are therefore always empty strings in graded results. The groups
reaper, notifiers, ZooKeeper, `kafka_zk` consumers, TLS/SASL/IAM, `/metrics` and log
contents are not graded; neither are the effects of `workers`, `queue-depth`,
`offsets-topic`, `[httpserver] timeout` or `[logging] filename`. Startup messages and exit codes are only checked as
"zero after a clean stop" and "non-zero with something on stderr" for failures.

## Observation and comparison rules

* Read results through the HTTP API with any client (e.g. Python `requests`).
* Evaluation depends on time: refresh intervals, `expire-cache`, the commit
  timestamps and wall-clock age of the last commit. Use short intervals and small
  windows when experimenting, and poll until the state you need has been reached.
  Graded scenarios space commits of one window about 1.5 s apart and either read
  the evaluation right after the last commit is stored or wait well past the
  relevant time span.
* JSON objects are compared key by key (exact key sets, exact strings, `true`/`false`
  distinct from numbers). Numbers are compared by value (`1`, `1.0` and `1e0` are
  equal); `complete` fractions within 1e-6.
* Order is not significant for name lists (`clusters`, `topics`, `consumers`,
  `modules`), for the keys of the `topics` map, or for the `partitions` list of an
  evaluation. Order *is* significant for `offsets` arrays (topic end offsets by
  partition; a partition's commit window from oldest to newest slot, including
  `null` slots).
* `timestamp`/`observedAt` values are only checked for plausibility (whole
  milliseconds near the time of the commit) and for relations between entries of
  the same window. `request.host` is environment dependent and ignored;
  `request.url` is the request path.
