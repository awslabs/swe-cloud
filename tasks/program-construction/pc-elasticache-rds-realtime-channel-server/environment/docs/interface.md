# realtime-channel-server — public interface

`realtime-channel-server` is a realtime server that speaks the Pusher protocol. One process
serves two things on the same port: a WebSocket endpoint for clients and a signed HTTP API
for trusted backends. Apps are stored in Postgres; an app record holds credentials, limits
and webhook targets. Valkey (a Redis-protocol server) carries fan-out between servers and a
cache. Webhook jobs pass through an Amazon SQS queue, and are then POSTed to the app's
webhook URLs.

The reference is `/reference/program`. Your build must produce `/app/program`, with the
same interface.

## Scope

In scope:

- the HTTP publish and query API listed below;
- WebSocket subscribe and publish on public, private and presence channels, using the
  Pusher v1 protocol (`protocol=7`, or no `protocol` parameter);
- the webhook events `channel_occupied`, `channel_vacated`, `member_added`,
  `member_removed` and `client_event`, delivered through the SQS driver;
- several servers that share one Valkey adapter prefix;
- the app cache.

Out of scope:

- push notifications, AI transport, delta compression, Ably compatibility, MCP,
  history/recovery;
- protocol V2 (`protocol=2`), user sign-in, watchlists, cache channels (names containing
  `cache-`), encrypted-channel semantics, channel namespaces, user-limited channels and
  wildcard subscriptions. Names that need namespaces, `#` or `*` are only checked for
  rejection;
- the `subscription_count` webhook and other webhook types;
- signed endpoints not listed below, for example `terminate_connections`;
- other queue, adapter and app drivers, clustering, metrics, and HTTP rate limiting;
- the app `policy` column. Tests leave it NULL and use the flat columns.

## Invocation

```
program [--config PATH]
program --help
```

`program` runs in the foreground until it receives SIGTERM or SIGINT, and logs to stdout.
`--help` prints usage and exits 0. An unknown argument makes it exit with status 2 and an
error on stderr.

Configuration is resolved in this order:

1. built-in defaults;
2. `config/config.toml` (or else `config/config.json`) relative to the working
   directory, if one is present;
3. the file given with `--config PATH`. A `.toml` file is read as TOML; any other file is
   read as JSON. This file replaces the earlier file as a whole; the two are not merged;
4. environment variable overrides (see below).

Before parsing, every `${NAME}` placeholder inside a configuration file is replaced with
the value of that environment variable. If a configuration file cannot be parsed, the
server keeps the previously resolved configuration. A value the server does not accept, such
as an unknown driver name, counts as a parse failure.

## Configuration used by the tests

The tests write configurations like the one below, in TOML or as the equivalent nested
JSON. They vary the values shown, and in particular `concurrency`, `fifo`,
`visibility_timeout`, `table_name`, the prefixes and the three optional sections at the
end.

```toml
host = "127.0.0.1"
port = 6001                 # WebSocket + HTTP API

[app_manager]
driver = "pgsql"            # TOML spelling

[database.postgres]
host = "<rds endpoint address>"
port = 5432
username = "<master user>"
password = "<master password>"
database = "<database name>" # must already exist
table_name = "applications"  # default

[adapter]
driver = "redis"
[adapter.redis]
prefix = "rt"                # namespace for the adapter's Valkey traffic

[cache]
driver = "redis"
[cache.redis]
prefix = "rt_cache:"         # namespace for cache keys

[queue]
driver = "sqs"
[queue.sqs]
region = "us-east-1"
endpoint_url = "http://engines:4566"
queue_url_prefix = "http://engines:4566/000000000000/mytest-"
visibility_timeout = 30      # seconds
wait_time_seconds = 1
concurrency = 5              # queue workers; 0 = enqueue only, never deliver
fifo = false

[metrics]
enabled = false

[rate_limiter]
enabled = false

# Optional sections
[webhooks.batching]
enabled = true               # default true
duration = 50                # ms
size = 100

[webhooks.retry]
enabled = true
max_attempts = 3             # unset = unlimited
initial_backoff_ms = 1000
max_backoff_ms = 60000
max_elapsed_time_ms = 300000

[app_manager.cache]
enabled = true
ttl = 300                    # seconds
```

The tests always set these environment variables:

- `REDIS_URL=redis://<valkey address>:<port>/0`, used by the adapter and the cache;
- `SHUTDOWN_GRACE_PERIOD=1`, the seconds to keep listening after a stop signal
  (default 10);
- `LOG_COLORS_ENABLED=false`.

Some tests also set `PORT`, which overrides the bind port, or variables that are referenced
by `${NAME}` placeholders. AWS credentials and `AWS_ENDPOINT_URL` come from the standard
environment.

## AWS resources

* **RDS Postgres.** The configured database exists before the server starts. At startup
  the server creates the app table (`table_name`) if it is missing. An existing table is
  kept with its rows; the server only adds the columns that later schema versions
  introduced, if they are missing. The server never writes app rows itself.
* **ElastiCache Valkey.** The tests use a Valkey replication group. Several servers may
  use the same Valkey instance with the same or different prefixes.
* **SQS.** Webhook jobs use the logical queue name `webhooks`. With `queue_url_prefix`,
  the queue URL is the prefix followed by `webhooks` (or `webhooks.fifo` when
  `fifo = true`), and the queue must already exist. The tests always provide a fresh
  queue and the prefix. Each job is one JSON message. A server that consumes the queue
  delivers any job it finds there, including jobs enqueued earlier, jobs enqueued by
  another server, and jobs the tests write in the same format. Tests read job messages
  from the queue, write job messages to it, and check queue counts.

## Apps (Postgres rows)

Each app is a row in the app table. The columns used in this scope are:

| Column | Type | Meaning |
| --- | --- | --- |
| `id` | varchar, PK | App ID used in HTTP API paths. |
| `key` | varchar, unique | Public key used by WebSocket clients and in signatures. |
| `secret` | varchar | Signing secret. |
| `max_connections` | integer, not null | Connection limit. |
| `enable_client_messages` | boolean | Allow `client-*` events. |
| `enabled` | boolean | Disabled apps refuse WebSocket connections. |
| `max_client_events_per_second` | integer, not null | Client event rate limit. |
| `max_presence_members_per_channel`, `max_presence_member_size_in_kb`, `max_channel_name_length`, `max_event_channels_at_once`, `max_event_name_length`, `max_event_payload_in_kb`, `max_event_batch_size` | integer, nullable | Optional limits; NULL means the built-in behaviour. |
| `webhooks` | jsonb | Array of webhook targets (below), or NULL. |

The startup schema has more columns than this table lists. The tests inspect the full
schema the server creates.

A webhook target looks like this:

```json
{"url": "http://sink.example:8080/hook",
 "event_types": ["channel_occupied", "channel_vacated", "member_added", "member_removed", "client_event"],
 "filter": {"channel_prefix": "presence-", "channel_suffix": "-x", "channel_pattern": "^room-[0-9]+$"},
 "headers": {"X-Custom": "value"}}
```

`event_types` is required. `filter`, and each key inside it, are optional, and so is
`headers`.

The server reads apps on demand. With `[app_manager.cache]` enabled, which is the
default, it caches resolved apps in Valkey under the cache prefix and in its own
memory, so a changed row stays in effect until the cached entry expires. Some query
responses are also cached briefly.

## HTTP API

All endpoints are under `/apps/{app_id}/…` and must be signed:

* `POST /apps/{app_id}/events`: body
  `{"name", "channel" | "channels": [...], "data", "socket_id"?, "info"?}`
* `POST /apps/{app_id}/batch_events`: body
  `{"batch": [{"name", "channel" | "channels", "data", "socket_id"?, "info"?}, ...]}`
* `GET /apps/{app_id}/channels`, with optional query parameters `filter_by_prefix` and
  `info`
* `GET /apps/{app_id}/channels/{channel_name}`, with optional query parameter `info`
  (`subscription_count`, `user_count`)
* `GET /apps/{app_id}/channels/{channel_name}/users` (presence channels)

`GET /up` and `GET /up/{app_id}` are unsigned health checks. They answer with a
plain-text status and an `X-Health-Check` header.

**Signing.** Add these query parameters:

- `auth_key`: the app key;
- `auth_timestamp`: Unix seconds;
- `auth_version`: `1.0`;
- `body_md5`: the lower-case hex MD5 of the raw body, on POST requests.

Then add `auth_signature = hex(HMAC-SHA256(secret, METHOD + "\n" + PATH + "\n" + QUERY))`.
Here `QUERY` is every other query parameter as `k=v`, sorted by key and joined with `&`.
Request bodies are JSON (`Content-Type: application/json`). Responses are JSON. Errors
carry an HTTP status of 400 or above, and usually a JSON body with `error`, `code` and
`status`.

## WebSocket protocol

Connect to `ws://<host>:<port>/app/{app_key}?protocol=7&client=<name>&version=<v>`.
Frames are JSON text messages of the form `{"event": ..., "channel"?: ..., "data": ...}`.

* The server greets with `pusher:connection_established`. Its `data` is a JSON-encoded
  string containing `socket_id` and `activity_timeout`.
* Clients send:
  * `pusher:subscribe` with `{"channel": ..., "auth"?: ..., "channel_data"?: ...}`;
  * `pusher:unsubscribe` with `{"channel": ...}`;
  * `pusher:ping`, which is answered with `pusher:pong`.
* The channel type is set by the name prefix. `private-…` and `presence-…` channels need
  `auth`; any other name is public.
  * private: `auth = "<key>:" + hex(HMAC-SHA256(secret, socket_id + ":" + channel))`
  * presence: `channel_data` is a JSON string `{"user_id": ..., "user_info"?: ...}` and
    `auth = "<key>:" + hex(HMAC-SHA256(secret, socket_id + ":" + channel + ":" + channel_data))`
* The server replies with `pusher_internal:subscription_succeeded` or with a
  `pusher:error` frame (`{"code", "message"}`). For a presence channel the success reply
  includes `{"presence": {"ids", "hash", "count"}}`. Presence subscribers also receive
  `pusher_internal:member_added` and `pusher_internal:member_removed`.
* A client event is `{"event": "client-<name>", "channel": <channel>, "data": ...}`.

## Webhooks

The server POSTs JSON bodies of this form:

```json
{"time_ms": 1700000000000, "events": [{"name": "channel_occupied", "channel": "my-channel"}]}
```

Each request has these headers:

- `Content-Type: application/json`;
- `X-Pusher-Key`, the app key;
- `X-Pusher-Signature`, which is `hex(HMAC-SHA256(app secret, raw request body))`;
- the target's custom `headers`.

A receiver signals success with a 2xx status.

## Shutdown

On SIGTERM the server closes its client connections and finishes its pending webhook work.
It then exits with status 0.

## Observation and equivalence rules

The tests compare results as follows.

* **JSON objects.** Member order is never significant.
* **Booleans and numbers.** Booleans are distinct from numbers. Numbers compare by value,
  so `1`, `1.0` and `1e0` are equal.
* **String-encoded `data`.** Server-generated `data` that is a JSON-encoded string is
  compared after decoding. This covers `connection_established`, subscription replies,
  member frames, and non-string data published through the API. String data published
  through the API is relayed verbatim and compared exactly. So is the `data` of client
  events, whatever its JSON type.
* **Error text.** Error codes and messages are compared exactly. This applies to error
  frames, API error bodies and health-check bodies. The tests check only the HTTP status
  for errors in JSON parsing, field types or the content type.
* **Order.** Presence `ids` and `users` lists are compared without order. Frames on one
  connection are compared in order for events that happen one after another. Events
  published to several channels at once are compared without order.
* **Webhook batches.** How events are split into webhook requests depends on the batching
  window. Tests compare the multiset of delivered events, except where a configuration
  fixes the split: batching disabled, or a long window with a small `size`.
* **Asynchronous state.** Webhooks, queue counts and cache entries change asynchronously.
  The tests poll for up to several seconds, and also wait to confirm that unexpected
  deliveries do not happen.
