# stream-pipe-tool — public interface

stream-pipe-tool is a command-line producer, consumer and metadata lister for
Apache Kafka–compatible brokers. It moves delimited text between stdin/stdout
(or files) and Kafka topics, and renders consumed records through a small
output format language or a one-line JSON envelope.

The reference build is `/reference/program`; your build must produce
`/app/program`. This guide fixes invocation syntax, the admitted scope and how
results are observed. The precise consequences of option combinations
(delimiter edge cases, key/NULL handling, offset resolution, end-of-partition
bookkeeping, committed positions, exit statuses, …) are part of the behaviour
to be discovered by running the reference.

## Invocation

```
program <options> [file1 file2 .. | topic1 topic2 ..]
```

Options are POSIX `getopt` style single letters (GNU argument permutation
applies, so operands may precede later options); an option argument may be
attached (`-K:`) or separate (`-K :`). `-h` prints help and exits 0.
Diagnostics and progress messages go to stderr; record data goes to stdout.

Exactly one mode is selected per run. Without a mode option the choice depends
on whether stdin is a terminal, so always pass the mode explicitly:

| Mode | Selector | Operands |
|---|---|---|
| Producer | `-P` | optional input files |
| Simple consumer | `-C` | — |
| Balanced group consumer | `-G <group-id>` | one or more topic names to subscribe to |
| Metadata list | `-L` | — |

## Connecting to the AWS venue (MSK)

The program speaks the Kafka wire protocol only; it never calls AWS APIs. The
Kafka cluster is an Amazon MSK cluster provisioned through the AWS API at
`AWS_ENDPOINT_URL` (`http://engines:4566`, region `us-east-1`, dummy
credentials). Plaintext listeners only; no TLS, no SASL.

1. Create a cluster, e.g. with boto3:
   `kafka.create_cluster(ClusterName=..., KafkaVersion="3.6.0", NumberOfBrokerNodes=1,
   BrokerNodeGroupInfo={"InstanceType": "kafka.t3.small", "ClientSubnets": ["subnet-a", "subnet-b"]})`.
2. Poll `describe_cluster` until `ClusterInfo.State == "ACTIVE"`.
3. `get_bootstrap_brokers(ClusterArn=...)["BootstrapBrokerString"]` is a
   `host:port[,host:port]` list; pass it unchanged with `-b <brokers>`
   (equivalently `-X bootstrap.servers=<brokers>` or a configuration file).
4. Create topics (with the partition counts you need) using a Kafka admin
   client, e.g. `confluent_kafka.admin.AdminClient.create_topics`, and wait
   until every partition has a leader. The graded scenarios always create
   their topics first; do not rely on broker-side auto-creation.
5. Delete the cluster with `delete_cluster` when done.

The cluster runs a single broker (replication factor 1).

`confluent-kafka` (Python, bundling librdkafka 2.6.1) is installed, as are the
Debian `librdkafka-dev` and `libyajl-dev` C libraries. Any of them may be used
by your implementation; the graders use confluent-kafka to create topics, to
write fixture records (including explicit timestamps), to read records back
(including headers and `isolation.level=read_committed`) and to list consumer
group offsets.

### Client configuration

`-X property=value` sets a client (librdkafka-style) configuration property;
properties prefixed with `topic.` apply to the topic configuration. A broker
list is required (`-b`, or `bootstrap.servers` / `metadata.broker.list` from
`-X` or a configuration file).

Configuration files contain `property=value` lines. `-F <file>` reads one
(`-F none` reads none); without `-F` the program reads the file named by
`$KCAT_CONFIG`, and otherwise `$HOME/.config/kcat.conf` if it exists. File
syntax details (comments, blank lines, whitespace, escapes) and failure
handling are part of the behaviour.

`-m <seconds>` bounds metadata/transaction-control waits. `-q` silences
informational stderr output, `-v` raises verbosity, `-E` keeps running after
non-fatal errors. These four options are accepted but not graded (they only
affect stderr and timeouts).

## Admitted scope

### Producer (`-P`)

| Option | Meaning |
|---|---|
| `-t <topic>` | destination topic (required) |
| `-p <partition>` | explicit partition; `-p -1` or omission uses the client partitioner |
| `-D <delim>` | message delimiter (default newline) |
| `-K <delim>` | key delimiter: the part of a message before the first occurrence is the key |
| `-k <str>` | fixed key |
| `-Z` | send empty key/value as NULL rather than zero-length |
| `-H name=value` | add a header to every message (repeatable, order preserved); `-H name` adds a header without a value |
| `-z snappy\|gzip\|lz4\|zstd\|none` | compression codec |
| `-l` | read the single file operand as delimited input (like stdin) |
| file operands without `-l` | each file is sent whole as one message |
| `-c <cnt>` | stop after producing this many messages |
| `-T` | echo produced input to stdout |
| `-X transactional.id=<id>` | transactional producer: all messages of the run in one transaction, committed at end of input |

Delimiter arguments (`-D`, `-K`, for producing and consuming) may be
multi-byte and translate exactly the escapes `\n`, `\t` and `\xNN`; any other
backslash sequence is taken literally.

Placement without `-p`: keyed messages go where the librdkafka default
partitioner (`consistent_random`, CRC32 of the key) puts them; NULL-key
messages are placed randomly and that placement is not graded.

### Simple consumer (`-C`)

| Option | Meaning |
|---|---|
| `-t <topic>` | source topic (required) |
| `-p <partition>` | consume only this partition (otherwise all partitions) |
| `-o <offset>` | start position, see grammar below |
| `-c <cnt>` | stop after this many messages |
| `-e` | exit when the end of the partition(s) is reached |
| `-f <fmt>` | output format string, see below |
| `-J` | output each record as a JSON envelope |
| `-D <delim>` | record delimiter for plain and `-J` output |
| `-K <delim>` | plain output prints the key, this delimiter, then the payload |
| `-Z` | plain/format output renders NULL keys/values as `NULL` |
| `-u` | unbuffered output (each record is written immediately) |

`-f` takes precedence over `-D`/`-K` for record layout. Consumption uses the
client defaults otherwise (e.g. `isolation.level=read_committed`); `-X`
properties such as `group.id` are passed to the client.

Offset grammar for `-o`:

```
beginning | end | stored
<N>         absolute offset
-<N>        relative to the end of each partition
s@<ms>      start at the first message with timestamp >= <ms> (ms since epoch)
e@<ms>      stop before the first message with timestamp >= <ms>
```

`s@` and `e@` may both be given (two `-o` options) to select a timestamp
window.

### Balanced group consumer (`-G <group-id> topic...`)

Joins consumer group `<group-id>` and subscribes to the listed topics. `-o`
(except the timestamp forms), `-c`, `-e`, `-f`, `-J`, `-D`, `-K`, `-Z`, `-u`
apply as for `-C`. The group's committed offsets are observable through the
broker (`AdminClient.list_consumer_group_offsets`).

### Metadata list (`-L`)

Lists brokers, topics and partitions (leader, replicas, ISRs). `-t <topic>`
restricts the listing to one existing topic. `-J` emits a single JSON document
instead of text. The text listing looks like:

```
Metadata for <topic or "all topics"> (from broker <id>: <name>):
 1 brokers:
  broker 0 at host:9092 (controller)
 1 topics:
  topic "orders" with 2 partitions:
    partition 0, leader 0, replicas: 0, isrs: 0
    partition 1, leader 0, replicas: 0, isrs: 0
```

The JSON document has members `originating_broker` (`{id, name}`), `query`
(`{topic}`), `controllerid`, `brokers` (`[{id, name}]`, name is `host:port`)
and `topics` (`[{topic, partitions: [{partition, leader, replicas: [{id}],
isrs: [{id}]}]}]`).

### Out of scope

Avro / Schema Registry deserialisation (`-s avro`, `-r`), the `-s` pack-string
deserialisers, query mode `-Q`, the mock cluster (`-M`), SSL/SASL, librdkafka
debug output (`-d`), the `-X list` / `-X dump` listings, `-O`, `-V` text, the
help text content, aborting a transaction through a termination signal, and
listing (`-L -t`) or producing to topics that do not exist.

## Output format language (`-f`)

The format string is written to stdout once per record with these tokens
substituted:

| Token | Substitution |
|---|---|
| `%s` | payload |
| `%S` | payload length in bytes (or `-1` for NULL) |
| `%R` | payload length (or `-1` for NULL) as a 4-byte big-endian signed integer |
| `%k` | key |
| `%K` | key length in bytes (or `-1` for NULL) |
| `%T` | message timestamp (ms since epoch) |
| `%h` | headers as `name=value` pairs separated by commas |
| `%t` | topic |
| `%p` | partition |
| `%o` | offset |
| `%%` | a literal `%` |
| `\n \r \t` | newline, carriage return, tab |
| `\xXX` | the byte with that hex value |

## JSON envelope (`-J`)

One JSON object per record with the members `topic`, `partition`, `offset`,
`tstype` (`create`, `logappend` or `unknown`), `ts`, `broker` (id of the broker
the record was fetched from), `key` and `payload` (strings, or `null` for
NULL), plus `headers` only when the record has headers: a flat array
alternating header name and value (`null` for a header without a value). Each
object is followed by the record delimiter (default newline).

## Observation rules

* Record data is the stdout byte stream and is compared exactly unless stated
  otherwise; stderr is diagnostic and never compared.
* JSON output is compared by decoded value: member order and whitespace inside
  an object are not significant, but JSON types are (string vs number vs
  boolean vs null). Numbers compare by numeric value (`1`, `1.0` and `1e0` are
  equal). Record payloads used with `-J` are valid UTF-8.
* In the `-L` text header line, the originating broker id and name are not
  compared; the rest of the listing is. Topic order in all-topic listings is
  not significant.
* Records of one partition are written in offset order. When several
  partitions are consumed in one run, the interleaving between partitions is
  not significant.
* Producer timestamps reflect wall-clock time; graders compare `%T`/`ts`
  against values read back from the broker or against fixture timestamps.
* Exit status is significant (0 for success, 1 for failures).
* Broker-side effects (records, keys vs NULL, headers, partition placement,
  transaction markers and read_committed visibility, committed group offsets)
  are observed with an independent Kafka client, both after successful runs
  and after refused ones.
* Runs without `-e`/`-c`/`e@` that would keep waiting are observed for several
  seconds and then killed; with `-u`, records already consumed must have been
  written by then.
