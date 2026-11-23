# descriptor-limiter: interface guide

`descriptor-limiter` is a long-running rate-limit service. Clients send it a
*domain* and a list of *descriptors* (ordered key/value entry lists). It looks up
the matching limit for each descriptor in its YAML configuration, counts the hit
in a fixed-window counter held in Redis-protocol storage (Valkey), and answers
whether each descriptor, and the request overall, is within its limit.

The reference build is `/reference/program`; your build must produce
`/app/program` with the same interface.

## Invocation

The service is a single executable with no command-line arguments. Configure
it entirely through environment variables. Start it in the background and stop
it with SIGTERM:

```bash
env RUNTIME_ROOT=/path/to/runtime RUNTIME_SUBDIRECTORY= RUNTIME_WATCH_ROOT=false \
    REDIS_SOCKET_TYPE=tcp REDIS_TYPE=single REDIS_URL=host:port \
    USE_STATSD=false DISABLE_STATS=true EXPIRATION_JITTER_MAX_SECONDS=0 \
    HOST=127.0.0.1 PORT=8080 GRPC_HOST=127.0.0.1 GRPC_PORT=8081 DEBUG_HOST=127.0.0.1 DEBUG_PORT=6070 \
    /reference/program &
```

The program only accepts requests after it has loaded its initial
configuration and connected to its storage. Poll `/healthcheck` until it
answers before you send traffic. The graders start the program this way, with
their own ports, and wait up to about 20 seconds for `/healthcheck`.

### Environment

| Variable | Meaning | Default |
|---|---|---|
| `HOST`, `PORT` | HTTP listener (`/json`, `/healthcheck`) | `0.0.0.0`, `8080` |
| `GRPC_HOST`, `GRPC_PORT` | gRPC listener | `0.0.0.0`, `8081` |
| `DEBUG_HOST`, `DEBUG_PORT` | debug listener (its content is not graded, but the variables are always set) | `0.0.0.0`, `6070` |
| `RUNTIME_ROOT` | configuration root | `/srv/runtime_data/current` |
| `RUNTIME_SUBDIRECTORY` | subdirectory under the root (may be empty) | empty |
| `RUNTIME_APPDIRECTORY` | directory, under root and subdirectory, holding the configuration files | `config` |
| `RUNTIME_WATCH_ROOT` | selects how configuration changes are noticed (see below) | `true` |
| `MERGE_DOMAIN_CONFIG` | `true` lets several files contribute to one domain | `false` |
| `REDIS_SOCKET_TYPE` | `tcp` or `unix` | `unix` |
| `REDIS_TYPE` | `single` (`cluster` is not graded) | `single` |
| `REDIS_URL` | `host:port` of the main storage | - |
| `REDIS_PERSECOND` | `true` sends `second`-unit limits to a second storage instance | `false` |
| `REDIS_PERSECOND_SOCKET_TYPE`, `REDIS_PERSECOND_TYPE`, `REDIS_PERSECOND_URL` | the second instance, same meanings as above | `unix`, `single`, - |
| `EXPIRATION_JITTER_MAX_SECONDS` | upper bound for random extra seconds added to counter expiry; `0` disables it | `300` |
| `CACHE_KEY_PREFIX` | string prepended to every counter key | empty |
| `STOP_CACHE_KEY_INCREMENT_WHEN_OVERLIMIT` | `true` changes which counters a request increments when it would go over a limit | `false` |
| `NEAR_LIMIT_RATIO` | affects statistics only; not graded | `0.8` |
| `LOG_LEVEL` | `debug`, `info`, `warn`, `error`; log output is not graded | `WARN` |
| `USE_STATSD`, `DISABLE_STATS` | stats sinks are out of scope; graders set `USE_STATSD=false DISABLE_STATS=true` | `true`, `false` |

Graders always pass `REDIS_SOCKET_TYPE=tcp`, `REDIS_TYPE=single`, and, when
the split is used, `REDIS_PERSECOND=true REDIS_PERSECOND_SOCKET_TYPE=tcp`.
Unless a test is about expiry jitter, they set
`EXPIRATION_JITTER_MAX_SECONDS=0`.

### Configuration files and reloads

The configuration is the set of files under
`RUNTIME_ROOT/RUNTIME_SUBDIRECTORY/RUNTIME_APPDIRECTORY`. Explore which files
in and around that directory are read. Each file defines one domain. Domains
must be unique across files unless `MERGE_DOMAIN_CONFIG=true`.

The configuration is reloaded while the service runs:

- `RUNTIME_WATCH_ROOT=false`: changes inside the configuration directory
  (writes, new files, removals) trigger a reload.
- `RUNTIME_WATCH_ROOT=true` (default): reloads follow replacement of
  `RUNTIME_ROOT` itself, typically an atomically swapped symlink whose target
  contains `RUNTIME_SUBDIRECTORY/RUNTIME_APPDIRECTORY`.

Graders allow about 10 seconds for a reload to take effect. Part of the
behavior to reproduce is what the service does with an invalid
configuration, both at startup and on reload. Observe it on the reference.

## Storage in the AWS venue

Storage is an Amazon ElastiCache for Valkey replication group created through
the emulated AWS API (`AWS_ENDPOINT_URL`, region `us-east-1`, dummy
credentials). Plaintext with no AUTH token is sufficient:

```python
import boto3
ec = boto3.client("elasticache")
ec.create_replication_group(ReplicationGroupId="rl-main", ReplicationGroupDescription="limits",
                            Engine="valkey", CacheNodeType="cache.t4g.micro", NumCacheClusters=1)
# poll describe_replication_groups until Status == "available"
ep = ec.describe_replication_groups(ReplicationGroupId="rl-main")["ReplicationGroups"][0]["NodeGroups"][0]["PrimaryEndpoint"]
# REDIS_URL = f"{ep['Address']}:{ep['Port']}"
```

In this venue the reported address can be `localhost`. If so, connect to the
host of `AWS_ENDPOINT_URL` (`engines`) on the reported port. To use the
per-second split, create a second group and point the `REDIS_PERSECOND_*`
variables at it. Graders create the groups themselves and pass their endpoints
to the program.

## Configuration language

```yaml
domain: <unique domain name>
descriptors:
  - key: <entry key, required>
    value: <entry value, optional; may contain '*' wildcards>
    rate_limit:                       # optional
      unit: second | minute | hour | day
      requests_per_unit: <uint>
      unlimited: <bool>               # instead of unit/requests_per_unit
      name: <optional rule name>
      replaces:                       # optional
        - name: <name of another rule>
    shadow_mode: <bool>
    detailed_metric: <bool>
    descriptors:                      # optional nested list, same shape
      - ...
```

- Each nested level matches the next entry of a request descriptor.
- A descriptor entry with no configured `value` matches any value for that key.
  A configured `value` matches only that value, or a pattern if it contains `*`.
  Work out how these alternatives rank against each other on the reference.
- A `rate_limit` with `unlimited: true` never limits.
- `shadow_mode` evaluates and counts as usual but never causes rejection.
- `replaces` lets a matched rule displace another matched rule that has the
  referenced `name`.
- `detailed_metric` only affects the statistics output, which is out of scope.
  The service accepts it, and it does not change counters or responses.
- Unknown keys, a missing key, an invalid unit and similar mistakes make the
  configuration invalid.

## Requests

### HTTP `/json`

`POST /json` with a JSON body. The body is the proto3 JSON mapping of
`RateLimitRequest` (field names in lowerCamelCase or snake_case):

```json
{"domain": "shop",
 "hitsAddend": 1,
 "descriptors": [
   {"entries": [{"key": "user", "value": "alice"}, {"key": "path", "value": "/cart"}]},
   {"entries": [{"key": "ip", "value": "10.0.0.1"}], "hitsAddend": "3"},
   {"entries": [{"key": "ip", "value": "10.0.0.2"}], "isNegativeHits": true}
 ]}
```

`hitsAddend` is optional at the request level and on each descriptor. A
descriptor-level value overrides the request-level value. If neither is given,
each request counts as one hit. `isNegativeHits: true` on a descriptor makes
its hits a refund instead of a charge.

The response body is the proto3 JSON mapping of `RateLimitResponse`:
`overallCode` and a `statuses` list with one entry per request descriptor, in
request order. Each entry has `code`, optional `currentLimit`
(`requestsPerUnit`, `unit`, `name`), `limitRemaining` and `durationUntilReset`
(a proto JSON duration string such as `"42s"`). Under the proto3 JSON mapping,
fields with default values are omitted. A missing `limitRemaining` means 0.

The HTTP status is 200 when the request is allowed and 429 when the overall code
is `OVER_LIMIT`. Both carry a JSON body with `Content-Type: application/json`.
Requests the service cannot parse or rejects get HTTP 400 with a plain-text
body.

### gRPC

The gRPC listener serves
`envoy.service.ratelimit.v3.RateLimitService/ShouldRateLimit` and the standard
`grpc.health.v1.Health` service. `/docs/rls_subset.proto` is a wire-compatible
subset of the public messages. It covers every field this guide mentions.
Compile it with the installed `grpc_tools`:

```bash
python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. rls_subset.proto
```

gRPC and `/json` are two encodings of the same operation and share counters.
A request the service rejects fails with a gRPC error status.

### `/healthcheck`

`GET /healthcheck` returns `200` with body `OK` while the service is healthy.

## Observing counters

Each counted descriptor uses one storage key:

```
<CACHE_KEY_PREFIX><domain>_<key1>_<value1>_<key2>_<value2>_..._<windowStart>
```

The key/value pairs are the request descriptor's entries. `<windowStart>` is the
Unix time, in seconds, at which the limit's current fixed window began (windows
are aligned to multiples of the unit length). The value is the decimal hit
count, and the key has an expiry. Counters for `second`-unit limits are stored
on the per-second instance when the split is enabled. All other counters are
stored on the main instance. You can inspect them with any Redis-protocol
client, for example `redis-py`, which is installed.

## What is graded, and how outputs are compared

Graded: `/healthcheck`, `/json`, gRPC `ShouldRateLimit` and the health
service's overall status, the configuration language above (including
validation), the environment variables in the table except where it says
otherwise, configuration reloads, and the resulting storage state: which keys
exist on which instance, their integer values and their expiry.

Not graded, so you need not implement them: the per-descriptor `limit`
override in requests; `week`, `month` and `year` units; configuration keys
`quota_mode`, `share_threshold`, `value_to_metric` and `metadata`; response
headers; the local over-limit cache (`LOCAL_CACHE_SIZE_IN_BYTES`); global
shadow mode; `REDIS_AUTH`; cluster mode; TLS; statistics sinks; the debug
listener; xDS configuration; and the memcache backend.

Comparison rules:

- JSON replies are parsed. Field order and whitespace do not matter. Output
  field names are lowerCamelCase. A field holding its default value (0, `""`)
  may be omitted or present, and an absent message field may also be `null`.
  Other fields must not be populated.
- Numbers are compared by value: `2`, `2.0` and `2e0` are equal. Booleans are
  never numbers, and numbers must be JSON numbers, not strings. Enums are their
  string names (`OK`, `OVER_LIMIT`, `HOUR`, ...).
- `durationUntilReset` is compared by value, for example `"42s"` and
  `"42.000s"` are equal. It must equal the time left in the current window as
  of the request, and it is absent when there is no `currentLimit`.
- HTTP status codes are exact (200, 429, 400). Error bodies are not compared.
  Rejected gRPC calls must fail with status `UNKNOWN`.
- Storage keys are compared as exact strings, values as integers, and expiry
  to within a few seconds.

Window boundaries make results time-dependent. Graders avoid running a test
across a window boundary for `minute`, `hour` and `day` limits. A request's
`second`-unit hits are judged within that request.
