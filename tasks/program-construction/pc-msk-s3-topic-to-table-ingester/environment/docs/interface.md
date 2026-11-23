# stream-lake-ingest — public interface

`stream-lake-ingest` consumes JSON messages from a Kafka topic and appends them to an
existing Delta Lake table stored in S3. Progress through the topic is recorded inside the
Delta table itself, so a later run against the same table continues where the earlier
one stopped. Messages that cannot be ingested can be diverted to a second Delta table
(the dead-letter table).

This guide fixes the command syntax, the AWS/Kafka setup and what is observable. How the
program reacts to particular inputs, timings and table states is to be established by
running the reference program.

## Invocation

```
program ingest [OPTIONS] <topic> <table_location>
program --help | --version
program ingest --help
```

`<topic>` is the Kafka topic to read. `<table_location>` is the Delta table URI, in this
venue `s3://<bucket>/<prefix>` (no trailing slash needed). Most options can also be
supplied through the environment variable shown in `ingest --help`; a command-line value
takes precedence over the environment.

Options in scope:

| Option | Meaning |
|---|---|
| `-k, --kafka <brokers>` | Kafka bootstrap servers (default `localhost:9092`). |
| `-K, --kafka_setting <name=value>` | Extra Kafka consumer property (librdkafka name); repeatable. |
| `-g, --consumer_group <id>` | Kafka consumer group id (default `kafka_delta_ingest`). |
| `-a, --app_id <id>` | Application identity used when recording progress in the Delta table (default `kafka_delta_ingest`). |
| `--seek_offsets <json>` | JSON object mapping partition number (string) to an offset, e.g. `{"0":123,"1":321}`; a *seek* point, used only when the table holds no progress for those partitions. |
| `-o, --auto_offset_reset earliest\|latest` | Where to start a partition that has neither stored progress nor a seek offset (default `earliest`). |
| `-l, --allowed_latency <seconds>` | Allowed time between consuming a message and committing it (default `300`). |
| `-m, --max_messages_per_batch <n>` | Maximum number of rows buffered into one in-memory record batch (default `5000`). |
| `-b, --min_bytes_per_file <n>` | Target minimum size in bytes of each written data file (default `134217728`). |
| `-t, --transform '<property>: <source>'` | Field transform applied to every message; repeatable. See *Transforms*. |
| `--dlq_table_location <uri>` | Delta table that receives dead letters. |
| `--dlq_transform '<property>: <source>'` | Transform applied to dead-letter records before they are written; repeatable. |
| `-c, --checkpoints` | Write Delta checkpoints (every 10th table version). |
| `-e, --ends_at_latest_offsets` | Bounded run relative to the partitions' latest offsets observed at start-up, instead of running indefinitely. |
| `--json <value>` | Message format selector: JSON (the default when no format option is given). Use `--json ''` or omit it; schema-registry URLs are out of scope. |

Out of scope: `--avro`, `--soe-avro`, schema registries, `--decompress_gzip`, Azure
storage, statsd metrics (`-s/--statsd_endpoint` may be left at its default; nothing needs
to listen there), SSL/SASL settings and multi-process consumer-group rebalancing.

Without `--ends_at_latest_offsets` the program runs until it is stopped or fails. The
exit status of a bounded run is observable.

## Venue setup

All AWS access goes to the emulator at `AWS_ENDPOINT_URL` (region `us-east-1`, dummy
credentials). The program additionally needs, in its environment:

```bash
export AWS_REGION=us-east-1          # if not already set
export AWS_ALLOW_HTTP=true           # the endpoint is plain http
export AWS_S3_LOCKING_PROVIDER=dynamodb
export DELTA_DYNAMO_TABLE_NAME=<lock table name>   # default name: delta_log
```

Commits to S3 must be coordinated through a DynamoDB table (the emulator does not support
the conditional S3 writes that would otherwise be used). Create it before the run:

```python
boto3.client("dynamodb").create_table(
    TableName="my-locks",
    AttributeDefinitions=[{"AttributeName": "tablePath", "AttributeType": "S"},
                          {"AttributeName": "fileName", "AttributeType": "S"}],
    KeySchema=[{"AttributeName": "tablePath", "KeyType": "HASH"},
               {"AttributeName": "fileName", "KeyType": "RANGE"}],
    BillingMode="PAY_PER_REQUEST")
```

**Kafka.** The broker is an Amazon MSK cluster created through the emulator's `kafka`
API; pass the `BootstrapBrokerString` returned by `get_bootstrap_brokers` to `--kafka`:

```python
kafka = boto3.client("kafka")
arn = kafka.create_cluster(
    ClusterName="demo", KafkaVersion="3.6.0", NumberOfBrokerNodes=1,
    BrokerNodeGroupInfo={"InstanceType": "kafka.m5.large", "ClientSubnets": ["subnet-1"]},
)["ClusterArn"]
# poll describe_cluster(ClusterArn=arn)["ClusterInfo"]["State"] until "ACTIVE"
brokers = kafka.get_bootstrap_brokers(ClusterArn=arn)["BootstrapBrokerString"]
```

Topics are created and messages produced with an ordinary Kafka client (the Python
`confluent_kafka` package is installed). Message values are the payloads; keys and
headers are not interpreted.

**Tables.** The data table, and the dead-letter table if used, must already exist: the
program appends to a table, it does not create one. A table is created by writing its
version-0 commit, `<prefix>/_delta_log/00000000000000000000.json`, as JSON lines with a
`protocol` action and a `metaData` action carrying the schema and partition columns.
A neutral example (one action per line; the `schemaString` value is a JSON string):

```json
{"commitInfo":{"timestamp":1700000000000,"operation":"CREATE TABLE","operationParameters":{"isManaged":"false","description":null,"partitionBy":"[]","properties":"{}"},"isBlindAppend":true}}
{"protocol":{"minReaderVersion":1,"minWriterVersion":2}}
{"metaData":{"id":"0b6f5d1e-8a43-4e4b-9a1e-5c1c8f3a9d10","format":{"provider":"parquet","options":{}},"schemaString":"{\"type\":\"struct\",\"fields\":[{\"name\":\"id\",\"type\":\"integer\",\"nullable\":true,\"metadata\":{}},{\"name\":\"name\",\"type\":\"string\",\"nullable\":true,\"metadata\":{}},{\"name\":\"date\",\"type\":\"string\",\"nullable\":true,\"metadata\":{}}]}","partitionColumns":["date"],"createdTime":1700000000000,"configuration":{}}}
```

Admitted column types are the Delta primitive types `string`, `integer`, `long`,
`short`, `byte`, `float`, `double`, `boolean`, `timestamp` and `date`, and nested
`struct` / `array` types built from them (`binary` and `decimal` columns are out of
scope). The table schema determines how each JSON value is stored. The format of the log and of checkpoints is described in
`delta-protocol.md` (only protocol version 1/2 tables without table features are in
scope).

## Transforms

Each `--transform` / `--dlq_transform` value has the form `PROPERTY: SOURCE` (split at
the first `:`; surrounding whitespace is trimmed). `PROPERTY` names the field to set,
dotted names address nested objects (`meta.kafka.offset`). `SOURCE` is either

* a JMESPath expression (https://jmespath.org/specification.html) evaluated against the
  message, with the additional functions `substr(value, start, length)`,
  `epoch_seconds_to_iso8601(n)`, `epoch_millis_to_iso8601(n)`,
  `epoch_micros_to_iso8601(n)` and `epoch_millis_to_micro(n)`; or
* one of the Kafka metadata names `kafka.partition`, `kafka.offset`, `kafka.topic`,
  `kafka.timestamp`, `kafka.timestamp_type`.

Example: `-t 'date: substr(meta.ts, `0`, `10`)' -t 'meta.kafka.offset: kafka.offset'`.
Dead-letter transforms run on dead-letter records, where Kafka metadata is not
available.

## Observable results

* The Delta log of each table: the sequence of commit files
  `_delta_log/<20-digit version>.json`, which versions exist and which commit holds
  what, their `add`, `txn`, `metaData` and `protocol` actions, and checkpoint files
  (`<version>.checkpoint.parquet`, `_last_checkpoint`) when checkpoints are enabled.
  An `add` must name an existing data file (its `path` is a URI-encoded path relative to
  the table root, per `delta-protocol.md`) with its correct `size` and a
  `partitionValues` entry for every partition column. How many data files a commit adds
  is observable.
* The rows of the Parquet data files referenced by `add` actions, together with the
  `partitionValues` recorded for each file. A row's partition-column value is the one
  recorded for its file, whether or not the file itself also stores that column. A
  column missing from a data file reads as null. Parquet column types must correspond to
  the Delta types (a `timestamp` column is a Parquet timestamp, a `date` column a Parquet
  date, and so on). Concretely, a file's column must read (via Arrow) as: `integer` int32,
  `long` int64, `short` int16, `byte` int8, `float` float32, `double` float64, `boolean`
  bool, `string` string (or large_string), `date` date32, `timestamp` a timestamp of any
  unit that is UTC-adjusted or has no zone, `array` a list (or large_list) and `struct` a
  struct whose present children follow the same rules.
* The dead-letter table's rows.
* Items in the DynamoDB lock table, one per commit file the program writes:
  `tablePath` (the table URI), `fileName`, `tempPath`, `complete` and `expireTime`.
* The exit status of every invocation that ends by itself: usage errors, rejected
  configuration, failures and bounded runs.

Incidental values are not part of the contract: data-file names and directory layout,
timestamps (`commitInfo.timestamp`, `txn.lastUpdated`, `modificationTime`), file
statistics, the presence and contents of `commitInfo`, the `tempPath` value, log text
written to stdout/stderr, the wording of dead-letter `error` texts (they must be
non-empty), and the order of rows within and across files and of actions within a
commit.

### Comparison rules

* Values are compared by the column's Delta type: numbers by numeric value (`2`, `2.0`
  and `2e0` are equal) but never equal to booleans; timestamps as UTC instants to the
  microsecond; strings exactly.
* Where a string column (at any depth) holds the JSON text of a value that was not a
  JSON string, the text is compared as JSON: key order and spacing are free, numbers
  compare by value. The dead-letter `json_string` field is compared the same way, and
  `base64_bytes` exactly. A dead letter's `timestamp` is the time it was created (checked
  to within a few seconds).
* Lock items: `tablePath` and `fileName` exactly, `complete` exactly as the string
  `"true"`, `tempPath` as a non-empty string, `expireTime` as a number of epoch seconds
  to within two minutes of one day after the commit.

### Test conditions

Each run gets a fresh consumer group; topics, tables and lock tables are created by the
tests as described above. Messages are produced before or during a run to explicit
partitions, with producer-assigned timestamps. Latencies are a few seconds and waits are
generous, but commit timing relative to the latency rule is observed. Byte thresholds
are only exercised with `-b 1` (any buffered data is enough) or the default (never
reached). Unbounded runs are ended with SIGTERM, sent to the `/app/program` process
itself, after their results are observed; that process must then exit (a wrapper script
should `exec` the real program), and nothing is graded after the signal. Another writer may commit to a table while a run is
active; it then writes the next commit file and registers a complete lock-table item for
it, as the program does.
