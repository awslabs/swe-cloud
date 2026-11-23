# redis-gateway: interface

`redis-gateway` is an HTTP (and WebSocket) server that translates request URLs into
commands for a Redis-protocol server and returns the replies in a selectable output
format. In this venue the Redis-protocol server is a Valkey cache provisioned through the
Amazon ElastiCache API.

## Invocation

```
program [CONFIG.json]
```

The program runs in the foreground and serves until it is terminated (SIGTERM/SIGINT).
With no argument it reads `webdis.json` from the current directory. Run it from a
writable directory, or point `logfile` at a writable path.

## Configuration file

A JSON object. Keys used in this task:

| key | type | meaning |
| --- | --- | --- |
| `redis_host` | string | host of the Redis-protocol server (default `127.0.0.1`) |
| `redis_port` | integer or string | its port (default `6379`) |
| `redis_auth` | `null`, string, or `["user","password"]` | credentials sent with `AUTH` (accepted; not graded, see below) |
| `database` | integer | default database number (default `0`) |
| `http_host` | string | listen address (default `0.0.0.0`) |
| `http_port` | integer or string | listen port (default `7379`) |
| `threads` | integer | worker threads |
| `pool_size` | integer | Redis connections per worker |
| `websockets` | boolean | enable the WebSocket endpoints (default `false`) |
| `acl` | array | access-control rules (see below) |
| `default_root` | string | request path substituted for a request to `/` |
| `http_max_request_size` | integer | maximum accepted request size in bytes (request line, headers and body together) |
| `verbosity`, `logfile` | integer, string | logging (not graded) |
| `daemonize` | boolean | keep `false` |

Any string value of the form `$NAME`, where `NAME` is all upper case, is replaced by the
value of that environment variable at startup, e.g. `"redis_host": "$REDIS_HOST"`,
`"redis_port": "$REDIS_PORT"`.

SSL connections to Redis, daemon mode, privilege dropping and log tuning are outside the
task scope.

## AWS / engine setup

`AWS_ENDPOINT_URL`, `AWS_DEFAULT_REGION=us-east-1` and dummy credentials are set. Create a
Valkey replication group (cluster mode disabled) and wait until it is `available`. Then
read its primary endpoint and turn it into a reachable host and port.

In this venue, the emulator reports the cache's data plane on a loopback `Address`
(`localhost`). From the program's container, that same `Port` is served by the host
named in `AWS_ENDPOINT_URL`. So connect to the `AWS_ENDPOINT_URL` host whenever the
reported address is loopback. Otherwise use the reported address as given:

```python
import os, time, urllib.parse
import boto3
ec = boto3.client("elasticache")
ec.create_replication_group(ReplicationGroupId="my-cache", ReplicationGroupDescription="demo",
                            Engine="valkey", CacheNodeType="cache.t4g.micro", NumCacheClusters=1)
while True:
    g = ec.describe_replication_groups(ReplicationGroupId="my-cache")["ReplicationGroups"][0]
    if g["Status"] == "available":
        break
    time.sleep(2)
ep = g["NodeGroups"][0]["PrimaryEndpoint"]           # e.g. {"Address": "localhost", "Port": 6379}
host, port = ep["Address"], ep["Port"]
if host in ("localhost", "127.0.0.1", "::1"):
    host = urllib.parse.urlparse(os.environ["AWS_ENDPOINT_URL"]).hostname
# put host/port into redis_host/redis_port (literally or via "$REDIS_HOST"/"$REDIS_PORT")
```

Delete the replication group when finished. The `redis` Python package is installed and
can be used to inspect the cache contents directly.

Venue notes: the emulated cache does not enforce credentials, so `redis_auth` has no
observable effect here and is not graded. On a newly opened connection to the cache, the
first command may have bytes that are not valid UTF-8 replaced in transit; graded writes
through the program therefore use UTF-8-valid data (which may include NUL, CR/LF, `/`, `?`
and multi-byte characters), while arbitrary bytes are graded on the read path.

## Request syntax

```
GET  /COMMAND/arg1/arg2/.../argN[.ext][?query]
POST /            with body  COMMAND/arg1/.../argN
PUT  /COMMAND/arg1/.../argN-1   with argN as the raw request body
GET  /DB/COMMAND/arg1/...       run on database number DB
```

* `/` separates arguments. Inside arguments, `%XX` escapes are decoded, so a literal `/`
  or `.` can be written `%2f` / `%2e`.
* An optional trailing extension selects the output format; it is not part of the last
  argument.
* Query parameters: `jsonp=NAME` or `callback=NAME` (JSONP wrapping of JSON output),
  `type=MIME/TYPE` (custom Content-Type, raw value output), `sep=STR` (separator between
  list elements for custom-type output).
* `GET`, `POST`, `PUT` and `OPTIONS` are accepted; other verbs are refused. Command
  responses carry an `ETag` (the double-quoted lowercase hex MD5 of the response body), and
  a matching `If-None-Match` request header yields `304`.
* Responses carry CORS headers; `GET /crossdomain.xml` serves a cross-domain policy file.
* The query parameter `filename=NAME` adds a `Content-Disposition` attachment header.

## Output formats

| extension | Content-Type | body |
| --- | --- | --- |
| none, `.json` | `application/json` | JSON object keyed by the command name |
| `.js` | `application/javascript` | as JSON |
| `.raw` | `binary/octet-stream` | the reply in Redis wire protocol (RESP) |
| `.msg` | `application/x-msgpack` | MessagePack map keyed by the command name (its own reply mapping; observe it) |
| `.txt`, `.html`, `.xhtml`, `.xml`, `.css`, `.png`, `.jpg`, `.jpeg`, `.bin` | matching type | the value itself |

JSON examples: `GET /SET/k/v` → `{"SET":[true,"OK"]}`, `GET /GET/k` → `{"GET":"v"}`,
`GET /INCR/n` → `{"INCR":1}`. How each kind of Redis reply (status, error, integer, bulk,
nil, multi-bulk, nested arrays, special commands) is represented in each format is part of
the behavior to observe. MessagePack is a public format (https://msgpack.org, spec at
https://github.com/msgpack/msgpack/blob/master/spec.md); the `msgpack` Python package is
installed for decoding.

## Access control

`acl` is an ordered list of rule objects:

```json
"acl": [
  { "disabled": ["DEBUG", "FLUSHALL"] },
  { "http_basic_auth": "user:password", "enabled": ["DEBUG"] },
  { "ip": "10.0.0.0/8", "disabled": ["*"] }
]
```

A rule may restrict the clients it applies to with `ip` (CIDR) and/or `http_basic_auth`
(`user:password` sent with HTTP Basic authentication); `enabled` / `disabled` list command
names, and `"*"` stands for every command. Commands are allowed unless a matching rule
disables them; when several rules match, later rules take precedence. A refused request
gets HTTP `403 Forbidden` and the command is not sent to Redis.

## Pub/Sub

`GET /SUBSCRIBE/channel` (and `PSUBSCRIBE`) keeps the response open with
`Transfer-Encoding: chunked`, delivering one formatted message per event, e.g.
`{"SUBSCRIBE":["subscribe","channel",1]}` followed by
`{"SUBSCRIBE":["message","channel","payload"]}` per published message. Each event is sent
as its own chunk, formatted with the request's output format; JSONP applies too. Graded
streams subscribe to a single channel or pattern per request.

## WebSockets

With `"websockets": true`, RFC 6455 clients may connect to `/` or `/.json` (JSON) or to
`/.raw` (Redis wire protocol). A JSON client sends a JSON array such as
`["SET","k","v"]` and receives the same JSON object an HTTP request would produce.
ACL rules apply to WebSocket commands too; they see the client address and the headers
of the opening handshake. A refused command yields
`{"message": "Forbidden", "error": true, "http_status": 403}` on JSON sockets and
`-ERR Forbidden\r\n` on raw sockets. The `websocket-client` Python package is installed.

## What is graded, and how outputs are compared

Tests start the program with a fresh configuration file, send HTTP requests with
`Connection: close` (one request per connection) or open WebSocket connections, and inspect
the cache independently.

* Status codes are compared exactly; reason phrases, `Server`, `Date` and `Connection`
  headers are not graded. `Content-Type` values are compared exactly. Non-streaming
  responses are read until the connection closes and must not use chunked encoding; when
  one has `Content-Length`, it must match the body.
* JSON bodies (and the JSON inside JSONP, WebSocket messages and stream chunks) are compared
  as JSON values: whitespace and object key order are free; numbers compare by numeric value
  (`1`, `1.0` and `1e0` are equal) but booleans and `null` are distinct from numbers;
  strings compare exactly. The JSONP envelope `NAME(` ... `);` followed by a newline is
  compared exactly.
* MessagePack bodies are decoded and compared like JSON; strings may use the str or bin
  family. Only flat replies (status, error, integer, bulk, nil, arrays of strings/nil, and
  `HGETALL`) are graded in MessagePack; nested arrays, command-specific shapes (streams, geo,
  `INFO`) and their `.raw` renderings are graded in JSON only.
* `.raw`, custom-type and other non-JSON bodies are compared byte for byte, as are chunk
  boundaries of streams (one event per chunk).
* WebSocket replies may be sent in text or binary frames.

Not graded: HTTP pipelining of several requests on one connection, several channels in
one HTTP `SUBSCRIBE` request, integer (non-string) elements in WebSocket JSON commands,
non-subscribe commands sent on a WebSocket after `UNSUBSCRIBE`, behavior while the cache is
unreachable other than plain HTTP requests, nil elements inside `.raw` arrays, `ip` rules with a `/0` mask, SSL, daemon mode,
privilege dropping, logging and reloading.
