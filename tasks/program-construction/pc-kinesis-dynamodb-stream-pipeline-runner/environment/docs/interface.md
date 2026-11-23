# lease-balanced-stream-bridge — interface

lease-balanced-stream-bridge is a single Linux x86-64 command-line program that runs a
declarative stream pipeline: one **input**, an optional list of **processors**, and one
**output**, described in a YAML file. This task is about its Kinesis input (DynamoDB
lease-balanced shard consumption with checkpointing) and its Kinesis output. The field
reference for both is in `components.md`. The reference build is `/reference/program`;
yours must be `/app/program`.

## Invocation

```
program run CONFIG.yaml          # graded: run the pipeline until the input ends or SIGTERM/SIGINT
program lint CONFIG.yaml         # exploration aids only (not graded):
program echo CONFIG.yaml         #   print the normalised config
program list [--format json-full] [inputs|outputs|processors|...]
program create aws_kinesis/mapping/file   # skeleton config with defaults
```

Only `run` is graded. The working directory is a writable scratch directory, and paths in the
config are relative to it. `run` keeps going while the input is live. A Kinesis input never ends
by itself. SIGTERM (or SIGINT) shuts down gracefully: in-flight messages finish, the shard
state is stored, and the exit status is 0. SIGKILL simulates a crashed instance. An input that
ends (for example `stdin` at end of file) makes `run` flush its output and exit with status 0.

Invalid configurations make `run` exit with status 1 before touching AWS: no checkpoint table is
created and no output is written. Examples are an unknown field, an unparsable duration,
`poll_period` above `lease_period`, a malformed `streams` entry, or balanced and explicit-shard
entries mixed in one input. A diagnostic on stdout or stderr names the offending field, value or
`streams` entry; for mixed entries it says that balanced and explicit shard streams cannot be
mixed. Problems that only show up when connecting are retried, and the process stays up. A
missing table with `create: false` and a stream that does not exist yet are both examples.

Logs are not graded. The reference writes `logfmt` logs to stdout, or to stderr when the
`stdout` output is used, so message data on stdout stays clean. With `logger: { level: DEBUG }`
it also logs shard claims, steals and closures. That is useful while exploring the reference.

The program starts an HTTP status server on `0.0.0.0:4195` by default. Every config used in
grading contains `http: { enabled: false }`.

## Configuration files used in grading

Environment variables are substituted into the raw YAML text before parsing. `${NAME}` is
replaced by the variable's value, and `${NAME:default}` falls back to `default` when the
variable is unset. `${! … }` is not substituted here: it is a per-message interpolation,
evaluated at runtime.

Graded configs use only this YAML subset: block mappings indented by two spaces; `key: value`
scalars (plain, `"double-quoted"` or `'single-quoted'`, booleans `true`/`false`, integers); flow
sequences of quoted strings (`streams: [ "a", "b:shardId-000000000001" ]`); the empty map `{}`;
block sequences of mappings (`- mapping: |`); and `|` literal block scalars. There are no
comments, anchors or multi-document files. Typical shapes:

```yaml
http:
  enabled: false
input:
  aws_kinesis:
    streams: [ "orders" ]
    endpoint: "http://engines:4566"
    region: "us-east-1"
    credentials:
      id: "test"
      secret: "test"
    dynamodb:
      table: "orders-checkpoints"
      create: true
    commit_period: "1s"
    rebalance_period: "1s"
    lease_period: "5s"
    steal_grace_period: "1s"
pipeline:
  processors:
    - mapping: |
        root.data = content().string()
        root.shard = meta("kinesis_shard")
output:
  file:
    path: "out.jsonl"
    codec: "lines"
```

```yaml
http:
  enabled: false
input:
  stdin:
    scanner:
      lines: {}
output:
  aws_kinesis:
    stream: "events"
    partition_key: 'user-${! json("key") }'
    hash_key: '${! json("h") }'
    endpoint: "http://engines:4566"
    region: "us-east-1"
    credentials:
      id: "test"
      secret: "test"
    max_in_flight: 1
    batching:
      count: 3
      period: "200ms"
```

### Admitted components and fields

* `input.aws_kinesis`: balanced or explicit-shard `streams` (names, ARNs, comma-separated
  elements, `stream:shardId`), plus `dynamodb` (`table`, `create`, `billing_mode`,
  `read_capacity_units`, `write_capacity_units`). Also admitted: `checkpoint_limit`,
  `commit_period`, `lease_period`, `rebalance_period`, `steal_grace_period`, `poll_period`,
  `start_from_oldest`, `auto_replay_nacks`, `batching` (`count`, `period`), and the
  connection fields. `enhanced_fan_out` is out of scope.
* `output.aws_kinesis`: `stream`, `partition_key` and `hash_key` interpolation,
  `max_in_flight`, `batching` (`count`, `period`), and the connection fields.
  `max_retries`/`backoff` are accepted, but failure and retry paths are not graded.
  That covers throttled or rejected records, oversized records and interpolation errors.
* Supporting components, used only to feed or observe the above:
  * `stdin` input with `scanner: { lines: {} }`: one message per line, without the newline.
  * `file` output with `codec: lines`: one message per line. `path` may contain
    interpolations, for example `'out/${! meta("kinesis_partition_key") }/records.jsonl'`.
    Missing parent directories are created. A write that fails (for example, a parent path
    exists as a regular file) rejects the message: a negative acknowledgement.
  * `stdout` output with `codec: lines`.
  * `mapping` processor, whose body is lines of the form `root.<name> = <expr>`. `<expr>` is
    `content().string()` (the message bytes as a string) or `meta("<key>")`. The result is a JSON
    object of those string fields and replaces the message.
* Interpolations (`${! … }` inside a string, mixed freely with literal text):
  `json("<key>")` gives the string value of a top-level key of the JSON message,
  `meta("<key>")` gives a metadata value, and `content()` gives the raw message.

Anything else the reference happens to list is out of scope, including `aws_kinesis_firehose`
and other inputs, outputs and processors.

Each message from the Kinesis input carries the metadata `kinesis_stream`, `kinesis_shard`,
`kinesis_partition_key` and `kinesis_sequence_number`.

## AWS endpoint and credentials

All AWS calls go to the emulator at `http://engines:4566` (also in `AWS_ENDPOINT_URL`), region
`us-east-1`, with dummy credentials (`test`/`test`). Graded configs state the connection
explicitly on each AWS component. The DynamoDB client uses the Kinesis connection settings
unless `dynamodb.region`/`endpoint`/`credentials` are given. The services are Kinesis Data
Streams and DynamoDB. Streams are created, and `ACTIVE`, before the program starts.

## What is observed, and how it is compared

* **Emitted messages**: the lines written by `file`/`stdout` outputs, decoded as JSON objects
  and compared as values: key order and spacing do not matter, and values are strings. Messages
  are compared as collections, keeping duplicates. Line order is not compared. Without crashes,
  thefts or takeovers, every record on a shard selected for consumption is emitted exactly
  once. Which shards are selected for consumption is part of the behaviour to reproduce.
* **The DynamoDB checkpoint table**, read with consistent `scan`/`get_item`: one item per
  `(StreamID, ShardID)`. All attributes are strings. The exact attribute set of each item, and
  the exact `StreamID`, `ShardID` and `SequenceNumber` values, are compared. `ClientID` may be
  any non-empty string, but it must be the same for every item one process holds and differ
  between processes. `LeaseTimeout` is an RFC 3339 timestamp, compared by value with a tolerance
  of a few seconds. Which attributes are present, and when, depends on the consumer's lifecycle
  and is part of the behaviour to reproduce. Tests also seed items as other clients would write
  them, for example `{"ClientID": "ghost", "LeaseTimeout": "2026-01-02T03:04:05.123456Z",
  "SequenceNumber": "…"}`, and check that items the program should not touch stay byte-identical.
* **Records written by the `aws_kinesis` output**, read back with boto3: the shard they landed
  on, the partition key, and the exact data bytes. Per-shard order is compared only when
  `max_in_flight: 1`.
* **Several concurrent instances, or instances and seeded rows**: how shards are divided over
  time, and how stopped, crashed or foreign clients' shards are taken over.

Lease and rebalance behaviour depends on timing. Graded configs use short periods, for example
`commit_period: 1s`, `rebalance_period: 1s`, `lease_period: 5s` and `steal_grace_period: 1s`.
They allow about a second of scheduling slack around the documented periods, and a few seconds
for anything to become visible. A closed shard that is still being read never reports its end
in this emulator, so what happens at shard end is not graded. This exclusion applies only
to reaching the end of a shard already being read. Streams may be resharded with
`SplitShard` before or during a run; discovery and claim selection, including which
shards get checkpoint items and are consumed, are part of the behaviour to reproduce.
