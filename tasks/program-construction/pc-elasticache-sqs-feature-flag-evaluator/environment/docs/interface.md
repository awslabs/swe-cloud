# flag-evaluator — interface

`flag-evaluator` is a long-running HTTP service that evaluates feature flags.
Flag definitions are read from a Redis-compatible store (Amazon ElastiCache, Valkey
engine) and single-flag evaluations are reported as feature events to an Amazon SQS
queue. The reference build is installed at `/reference/program`; your build must produce
`/app/program` with the same interface.

Companion guides:

- [flag-format.md](flag-format.md) — how a flag is written, the targeting query syntax, splits
- [http-api.md](http-api.md) — the HTTP evaluation endpoints
- [events.md](events.md) — the feature events written to SQS

## Invocation

```
program --config /path/to/relay.yaml
```

The only command-line option in scope is `--config PATH`, a YAML configuration file.
The process runs in the foreground, logging JSON lines to stdout/stderr, until it
receives SIGTERM/SIGINT; it then stops serving and exits with status 0. If the configuration is invalid (unreadable or malformed YAML, no
retriever, a retriever without its mandatory fields, or an SQS exporter without
`queueUrl`) the process logs the error and exits with a non-zero status without serving
requests. What the process does when the store cannot be read at start-up, and when a
later refresh fails, is behaviour to observe.

## Configuration file (admitted subset)

```yaml
server:
  port: 18031            # HTTP listen port (default 1031), all interfaces
hideBanner: true         # do not print the start-up banner
logLevel: info           # optional: debug | info | warn | error
pollingInterval: 1000    # milliseconds between refreshes of the flag store (default 60000)
retrievers:
  - kind: redis
    redisOptions:
      addr: "HOST:PORT"  # mandatory; the ElastiCache endpoint address and port
      db: 0              # optional database number (default 0)
    redisPrefix: "app1:" # optional key prefix
exporters:
  - kind: sqs
    queueUrl: "QUEUE_URL"   # mandatory; as returned by SQS CreateQueue/GetQueueUrl
```

Notes on the fields:

- The retriever options are spelled exactly `redisOptions` and `redisPrefix`.
  `redisOptions` also accepts `password` and `username`; the admitted scope uses an
  unauthenticated cache.
- `redisPrefix` selects the keys holding flags: every key starting with the prefix is
  one flag, and the flag's name is the key with the prefix removed. Keys that do not
  start with the prefix are ignored. Without a prefix every key of the database is a flag.
- Each flag key holds a string value: the flag definition as a JSON object.
- `pollingInterval` is in milliseconds. The store is read once at start-up and then
  again on each interval; a definition changed in the store becomes visible only after a
  later refresh.
- The `exporters` list may be omitted; then no events are sent.

Other configuration features of the program (API keys, other retrievers or exporters,
notifiers, flag sets, configuration-file hot reload, environment-variable overrides,
OpenTelemetry, swagger, monitoring port) are outside the admitted scope.

## AWS venue

The program reaches AWS only through the AWS API endpoint given by the standard SDK
environment: `AWS_ENDPOINT_URL`, `AWS_REGION`/`AWS_DEFAULT_REGION` and
`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` (dummy values are fine). Nothing AWS-related
is configured in the YAML except the queue URL.

Typical setup with boto3:

```python
import boto3, time
ec = boto3.client("elasticache")
ec.create_replication_group(ReplicationGroupId="flags-1", ReplicationGroupDescription="flags",
                            Engine="valkey", CacheNodeType="cache.t3.micro", NumCacheClusters=1)
while True:
    g = ec.describe_replication_groups(ReplicationGroupId="flags-1")["ReplicationGroups"][0]
    if g["Status"] == "available":
        break
    time.sleep(2)
ep = g["NodeGroups"][0]["PrimaryEndpoint"]          # {"Address": ..., "Port": ...}
queue_url = boto3.client("sqs").create_queue(QueueName="flag-events-1")["QueueUrl"]
```

The cache endpoint is used verbatim as `redisOptions.addr` (`Address:Port`), plain TCP
and no AUTH. Flags are written with any Redis client (the `redis` Python package is
installed), e.g. `redis.Redis(host, port).set("app1:my-flag", json.dumps(flag))`.
Clean up with `delete_replication_group` and `delete_queue`.

## Observation rules

- Evaluation results are observed through the HTTP status code and the JSON response
  body. JSON is compared as decoded JSON: object key order is not significant, but the
  set of keys is; arrays are compared in order. Booleans, strings, numbers, `null`,
  arrays and objects are distinct kinds; numbers are compared by value (`1`, `1.0` and
  `1e0` are the same number).
- A `metadata` member that is absent is equivalent to an empty `metadata` object.
- `errorDetails` is free-form text: when present it must be a string, but its content
  and its presence are not graded. `errorCode` values are graded exactly.
- On `/v1/...` endpoints, `timestamp` and `cacheable` are not graded; for a `400`
  response from those endpoints only the status code is graded.
- Events are observed by receiving messages from the SQS queue and decoding each
  message body as JSON. Messages on a standard queue are compared as an unordered
  collection (multiplicity matters). `creationDate` is checked to be a number of seconds
  close to the evaluation time, not an exact value.
- Log output, the start-up banner, response headers, and the program's version string are
  not part of the observable contract.
