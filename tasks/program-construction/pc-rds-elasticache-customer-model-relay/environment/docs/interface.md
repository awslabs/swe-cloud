# customer-model-relay — interface

`customer-model-relay` is a multi-tenant HTTP server that sells access to AI
models (mainly one chat model). Several customers (ordinary users) share one server. Each customer owns
API keys ("tokens") with their own quota and model allowlist, and each customer
belongs to a group that decides which upstream channels may serve their requests.
Administrators manage users, channels, redemption codes and logs; the root user also
manages server options. The server keeps its records in PostgreSQL and its per-user/per-key
caches and request rate limits in Valkey. The upstreams are OpenAI-compatible HTTP services.

Two tenants must never leak into each other: a customer sees and manages only
their own tokens, is charged only for their own requests, and is routed only to
channels of their own group.

The reference implementation is `/reference/program`. Your build produces
`/app/program`; `/app/build.sh` is run in a fresh container that has only `/app`.

## Invocation

```
program [--port PORT] [--log-dir DIR] [--version] [--help]
```

* `--port` — TCP port of the HTTP server (default `3000`; the `PORT` environment
  variable overrides it).
* `--log-dir` — directory for log files (default `./logs`, created if missing).
* `--help`, `--version` — print and exit.

The server runs in the foreground until terminated. It is ready when
`GET /api/status` answers HTTP 200 with `{"success": true, ...}`.

## Environment

| Variable | Meaning |
|---|---|
| `SQL_DSN` | `postgres://USER:PASSWORD@HOST:PORT/DBNAME?sslmode=disable`. On start the server creates or migrates its tables in this database. |
| `REDIS_CONN_STRING` | `redis://HOST:PORT[/DB]` of the Valkey server. Use the given database index. |
| `SYNC_FREQUENCY` | Seconds. Required together with `REDIS_CONN_STRING` for the cache to be used. It is the lifetime of cache entries and the period at which channel and option rows are re-read from the database. |
| `SESSION_SECRET` | Secret for session cookies; set any non-example value. |
| `INITIAL_ROOT_ACCESS_TOKEN` | Optional. On first start against an empty database, an administrator account (`root`) is created and given this management access token. |
| `GLOBAL_API_RATE_LIMIT` | Optional per-client request limit for `/api` routes per 3 minutes (default 240). The request history behind it lives in Valkey (see *Cache entries*). |
| `TIKTOKEN_CACHE_DIR` | Directory of tokenizer data for the reference. The reference copy lives in `/reference/tiktoken`. Implementations may ignore it. |

Do not set `BATCH_UPDATE_ENABLED`; quota and usage counters are written to the
database directly.

### Graded configuration

The tests start `/app/program --port P --log-dir D` once (restarting it only if it
exits), with a fresh, empty database in `SQL_DSN`, `REDIS_CONN_STRING` pointing at
a non-zero Valkey database index, `SYNC_FREQUENCY=5`, `SESSION_SECRET`,
`GLOBAL_API_RATE_LIMIT` set very high, and `TIKTOKEN_CACHE_DIR=/app/tiktoken` (which
need not exist in your build). The cache is therefore always enabled when graded;
running without Valkey is not graded. `INITIAL_ROOT_ACCESS_TOKEN` is not set.

Some tests use a **second instance**, started the same way but with its own empty
database, Valkey database index `5` and `GLOBAL_API_RATE_LIMIT=25`. Its rate-limit tests
empty that Valkey database before each test, and its option tests change server options
there. Both instances see requests from `127.0.0.1`.

## AWS venue

The database and cache are AWS-managed resources inside the Floci emulator
(`AWS_ENDPOINT_URL`, region `us-east-1`, dummy credentials):

* **RDS PostgreSQL** — create a DB instance (`Engine="postgres"`), wait until it
  is `available`, and use `Endpoint.Address`/`Endpoint.Port` plus the master
  credentials in `SQL_DSN`. Create extra databases with `CREATE DATABASE` as needed.
* **ElastiCache Valkey** — create a replication group (`Engine="valkey"`), wait
  until it is `available`, and use
  `NodeGroups[0].PrimaryEndpoint.Address`/`Port` in `REDIS_CONN_STRING`
  (no password, no TLS).

The program talks to these engines only through the endpoints AWS returns.
`psycopg` and `redis` (redis-py) are installed for Python and are what the tests use
to provision and inspect state.

## Fixtures provisioned by the administrator

Users, groups, channels and options are set up by an administrator, not through
the customer API. Tests write them directly into the database **after**
`/api/status` first succeeds, so your migration must have created these tables
by then. Tests rely on exactly these tables and columns (other columns may exist
and must have defaults):

* `users(id, username, password, display_name, role, status, access_token,
  quota, used_quota, request_count, "group", aff_code, inviter_id, ...)` — `id` is generated
  by the database (integer). Ordinary customers have `role = 1`, administrators
  `role = 10`, root users `role = 100`; tests insert all three the same way (the server's
  own first-start account may also exist). `status = 1` enabled, `2` disabled, `3` deleted. `access_token` (32 characters) is the customer's
  management credential. `quota` (bigint) is the remaining balance in quota
  units; `used_quota` (bigint) and `request_count` (integer) start at 0 and
  accumulate usage. Tests insert `username, password, display_name, role, status,
  access_token, quota, "group", aff_code` with a placeholder `password`; accounts
  that log in with a password are created through registration or the administrator API,
  which store passwords hashed.
* `channels(id, type, key, status, name, base_url, models, "group", priority,
  weight, created_time, ...)` — `id` generated. `type = 1` is an
  OpenAI-compatible upstream at `base_url`, authenticated with `key`. `models`
  and `"group"` are comma-separated lists. `status = 1` is enabled, `2` disabled.
  `priority` and `weight` are integers.
* `abilities("group", model, channel_id, enabled, priority)` — one row per
  (group, model, channel) combination a channel serves; `enabled` is boolean.
* `options(key, value)` — server options, `key` unique (tests upsert with
  `ON CONFLICT (key)`). `GroupRatio` holds a JSON object mapping group names to
  price multipliers. Other options (pricing tables such as `ModelRatio`, `SystemName`,
  `Notice`, `QuotaForNewUser`, `RegisterEnabled`, ...) keep the reference's defaults unless
  a test changes them through the option API.
* `redemptions(id, user_id, key, status, name, quota, created_time, redeemed_time)` —
  written by the server; tests read it. `status` 1 enabled, 2 disabled, 3 used.
* `logs` — usage and top-up records, read through the log API only.
* `tokens(id, user_id, key, status, name, created_time, accessed_time,
  expired_time, remain_quota, unlimited_quota, used_quota, models, subnet)` —
  written by the server; tests read it and sometimes set `status`/`used_quota`
  directly. `unlimited_quota` is boolean, quota columns bigint.

Channel and option rows are picked up at start and then every `SYNC_FREQUENCY`
seconds; tests wait for one refresh after provisioning them. Users may be added
or changed at any time (changes become visible as described under *Cache entries*).

Most channels serve only **`gpt-3.5-turbo`**. Some groups' channels serve several
models at once: chat (`gpt-3.5-turbo`, `gpt-4`), text completion (`text-davinci-003`),
embedding (`text-embedding-ada-002`), moderation (`text-moderation-latest`), image
(`dall-e-2`, `dall-e-3`) and speech (`tts-1`) models, plus names that are not built-in
models. Abilities may also be inserted for groups whose models are only listed, never relayed.

## Customer management API

Every request carries the customer's management credential:

```
Authorization: <users.access_token>
```

Responses are JSON envelopes `{"success": bool, "message": string, "data": ...}`.
A refused or failed operation is reported in the envelope with `success: false`
and a non-empty `message`; its HTTP status is not necessarily 4xx (observe the
reference). Refusals must leave stored rows unchanged.

| Method & path | Purpose |
|---|---|
| `GET /api/token/?p=N[&order=...]` | List the caller's tokens, page `N` (from 0). `order` may be `remain_quota` or `used_quota`; anything else is the default order. |
| `GET /api/token/search?keyword=K` | The caller's tokens whose name starts with `K`. |
| `GET /api/token/{id}` | One of the caller's tokens. |
| `POST /api/token/` | Create a token: `name`, `remain_quota`, `unlimited_quota`, `expired_time` (Unix seconds, `-1` = never), optional `models` (comma-separated allowlist; empty = any) and `subnet` (comma-separated CIDRs). `data` returns the stored token including its `key`. |
| `PUT /api/token/` | Update a token identified by `id` (same fields). `?status_only=...` (any non-empty value) updates only `status`. |
| `DELETE /api/token/{id}` | Delete one of the caller's tokens. |
| `GET /api/user/self` | The caller's own account record (`id`, `username`, `display_name`, `role`, `status`, `group`, `quota`, `used_quota`, `request_count`, `access_token`, ...; never the password). |
| `PUT /api/user/self` | Change the caller's `display_name` and/or `password`. |
| `DELETE /api/user/self` | Delete the caller's own account. |
| `GET /api/user/token` | Replace the caller's management access token; `data` is the new one. |
| `GET /api/user/aff` | The caller's affiliate code. |
| `GET /api/user/available_models` | Models the caller's group can use. |
| `POST /api/user/topup` | Redeem a code: `{"key": ...}`; `data` is the quota added. |
| `GET /api/log/self?p=N[&type=&token_name=&model_name=&start_timestamp=&end_timestamp=]` | The caller's own log entries, page `N`, optionally filtered. |
| `GET /api/log/self/stat?...` | A quota total over the caller's log entries; which entries and query filters count is reference behaviour. |
| `GET /api/log/self/search?keyword=K` | The caller's log entries matching `K` (what `K` is matched against is reference behaviour). |

### Registration and sessions

`POST /api/user/register` (`username`, `password`, optional `aff_code` of an inviting
user), `POST /api/user/login` (`username`, `password`) and `GET /api/user/logout` are in
scope. A successful login sets a `session` cookie; requests carrying that cookie instead
of an `Authorization` header are authenticated as that user for every management and
administrator route. Registration and login are also subject to a stricter per-client
limit of their own, kept in Valkey. Email verification, password-reset mail, OAuth and
captcha checks are off and not graded.

Token objects in responses have exactly the fields of the `tokens` table: `id`,
`user_id`, `key`, `status` (1 enabled, 2 disabled, 3 expired, 4 exhausted),
`name`, `created_time`, `accessed_time`, `expired_time`, `remain_quota`,
`unlimited_quota`, `used_quota`, `models`, `subnet`. A key is 48 ASCII
letters/digits and is stored without the `sk-` prefix. Token names do not have
to be unique, even within one customer. Which request fields a client may set,
validation limits, page size, list orders, and the rules for updating and
re-enabling tokens are part of the behaviour to discover from the reference.

Usage log entries carry at least `user_id`, `created_at`, `type` (1 top-up, 2
consumption, 3 administrator change), `content` (free text), `username`, `token_name`,
`model_name`, `quota`, `prompt_tokens`, `completion_tokens` and `channel` (id of
the channel that served the request).

## Administrator API

Same envelope and `Authorization: <access_token>` (or session) authentication; the caller
needs `role >= 10`, and `role = 100` for options. Which actions an administrator may take
on users of equal or higher role, and what each refusal leaves behind, are graded as the
reference behaves.

| Method & path | Purpose |
|---|---|
| `GET /api/user/?p=N`, `GET /api/user/search?keyword=K`, `GET /api/user/{id}` | Read user records. |
| `POST /api/user/` | Create a user (`username`, `password`, `display_name`). |
| `PUT /api/user/` | Update a user identified by `id` (`username`, `display_name`, `group`, `quota`, `password`). |
| `POST /api/user/manage` | `{"username", "action"}` with action `disable`, `enable`, `delete`, `promote` or `demote`. |
| `DELETE /api/user/{id}` | Delete a user. |
| `GET /api/channel/?p=N`, `GET /api/channel/search?keyword=K`, `GET /api/channel/{id}` | Read channels. |
| `POST /api/channel/` | Create channels (`type`, `key`, `name`, `base_url`, `models`, `group`, `priority`, `weight`, ...). |
| `PUT /api/channel/` | Update a channel identified by `id`. |
| `DELETE /api/channel/{id}` | Delete a channel. |
| `GET /api/channel/test/{id}[?model=M]` | Send a test request through the channel to its upstream. |
| `GET /api/redemption/?p=N`, `GET /api/redemption/search?keyword=K`, `GET /api/redemption/{id}` | Read redemption codes. |
| `POST /api/redemption/` | Create `count` codes named `name` worth `quota` each; `data` lists the new keys. |
| `PUT /api/redemption/[?status_only=1]` | Update a code identified by `id`. |
| `DELETE /api/redemption/{id}` | Delete a code. |
| `POST /api/topup` | `{"user_id", "quota", "remark"}`: add quota to a user directly. |
| `GET /api/log/?p=N[&type=&username=&...]`, `GET /api/log/stat?...` | All users' log entries and quota totals. |
| `GET /api/group/` | Group names that have a configured ratio. |
| `GET /api/option/`, `PUT /api/option/` | Root only: list options; update one `{"key", "value"}`. |

`GET /api/status` and `GET /api/notice` (no authentication) reflect some options.
Channels written through this API take part in routing after the next channel refresh,
exactly like channels provisioned in SQL. Server options changed through the API apply
to the instance that received the change at once. Channel balance queries, bulk channel
tests and bulk deletion of disabled channels are not graded.

## Relay API

```
POST /v1/chat/completions
Authorization: Bearer sk-<tokens.key>
Content-Type: application/json

{"model": "<model>", "messages": [...]}
```

The same key authenticates the other OpenAI-style endpoints: `POST /v1/completions`,
`/v1/embeddings`, `/v1/moderations`, `/v1/images/generations` and `/v1/audio/speech`,
streamed chat (`"stream": true`, server-sent events), the model listing
`GET /v1/models` and `GET /v1/models/{model}`, and the routes the reference declares but
does not implement (image edits/variations, files, fine-tuning, assistants, threads).
Upstream stubs answer each endpoint in its usual OpenAI shape and report `usage` on
every reply that is priced by tokens, including the last event of a stream; images and
speech are priced from the request itself. Audio transcription/translation and
`/v1/edits` are not graded.

For chat, the server authenticates the key,
checks the token, the owning user and the model allowlist, and chooses an
enabled channel of the user's group that serves the model. It forwards the
request to `<base_url>/v1/chat/completions` with `Authorization: Bearer
<channel key>` and returns the upstream's completion unchanged. Usage reported
by the upstream (`usage.prompt_tokens`, `usage.completion_tokens`) is priced
with the model's price and the group's `GroupRatio` and charged to the calling
user and token only. Relay failures use the envelope
`{"error": {"message": ..., "type": ..., ...}}` with an HTTP error status;
upstream failures are relayed with the upstream's status.

How variants of the `Authorization` header are interpreted, the exact pricing
and rounding per model and endpoint (the root option listing exposes the price
tables), how priority and weight influence channel choice, what happens
when a balance is low, which models a key lists, and the status/`type`/`code` of each
refusal are graded as the reference behaves; explore them.

## Cache entries

With the cache enabled, the server keeps entries for each user and key in Valkey.
Each entry lives for `SYNC_FREQUENCY` seconds:

* `token:<key>` — JSON copy of the token record.
* `user_quota:<user id>` — the user's quota as a decimal integer.
* `user_group:<user id>` — the user's group name.
* `user_enabled:<user id>` — `1` or `0`.
* `group_models:<group>` — the group's model names, comma-separated.

These entries can lag behind the database until they expire. The reference
behaviour defines when they are created, updated, decremented or used.

Rate limits keep each client's recent request times in Valkey lists
`rateLimit:GA<client ip>` (all `/api` routes) and `rateLimit:CT<client ip>`
(registration and login), with their own lifetime. Tests read these lists' length and
lifetime, delete them, and check which requests are refused with HTTP 429; they never
write entries into them.

## How results are compared

* HTTP status codes, `success`, `data`, relay `error.type` and (where the
  reference sends one) `error.code` are compared. Message texts are never
  compared beyond being non-empty strings.
* JSON numbers compare by value (`63`, `63.0`, `6.3e1` are equal); booleans are
  distinct from numbers. Token `models`/`subnet`: `null` and `""` both mean unset.
* Unordered results (search) are compared as multisets; documented orders
  (lists, logs, model listings, available models) are compared in order.
  Streamed replies are compared as the sequence of event payloads; speech replies
  as bytes and content type. A channel test's `time` is not compared. Channel choice is checked as the set of
  channels reached over many requests, never as a sequence.
* Usage counters may land shortly after the relay response; tests poll for up to
  ten seconds. Tests never require a stale cache entry to be served, nor a change
  to apply before the affected entries expire; they do check the state after
  expiry and the entries' contents and lifetimes.
* Upstream stubs accept bodies with `Content-Length` or chunked encoding.

## Out of scope

The web dashboard, email and password-reset mail, OAuth and other third-party
logins, captcha, external payment links, channel balance queries, upstream types
other than OpenAI-compatible (`type = 1`), audio transcription/translation and edits,
batch quota updates, running without the cache, and multi-node deployment.
