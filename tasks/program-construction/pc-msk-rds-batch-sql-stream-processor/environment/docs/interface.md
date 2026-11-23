# sql-stream-sink: program interface

sql-stream-sink runs a streaming pipeline. It consumes JSON records from Kafka
topics in batches and runs a DuckDB SQL statement over each batch. It writes
the result rows to a Kafka topic or a Postgres table. It also checks pipeline
configuration files without running them.

The program is one executable, called `program` in this guide. The process
name it reports in usage text is `sqlflow`.

## Invocation

```
program run <config> [--max-msgs N] [--stats-json PATH]
program run -c <config> [--max-msgs N] [--stats-json PATH]
program validate <config> [--json]
program config validate <config>
program --help | program <command> --help
```

| Flag | Meaning |
|---|---|
| `-c`, `--config` | Path to the pipeline config. The path can also be given positionally. |
| `--max-msgs N` | Stop after N consumed messages. `0` means no limit. `--max-msgs-to-process` is an alias. |
| `--stats-json PATH` | On exit, write the run's final counters as one JSON object to PATH. |

Without `--max-msgs`, `run` consumes until it receives `SIGTERM` or `SIGINT`.
It then shuts down in an orderly way and exits.

Diagnostics and logs go to standard error. The `console` sink writes result
rows to standard output, one JSON object per line. Logs are not part of the
graded interface. Set `SQLFLOW_LOG_LEVEL` (`DEBUG`, `INFO`, `WARN`, `ERROR`)
to change how much is logged.

The following are outside the admitted scope: `serve`, `rollup`, `tail`,
`dev`, `turbostats` and `completion`, and the `run` flags `--metrics`,
`--pprof`, `--turbostats` and `--with-http-debug`. `run` may log that it is
serving a health endpoint on port 8000. If it cannot bind the port, it logs
the failure and keeps running.

## Exit status

| Exit | Meaning |
|---|---|
| 0 | Success. This includes an orderly shutdown on a signal or after `--max-msgs`. |
| 1 | An internal or system failure that is not classified more precisely. |
| 10 | A user error, such as an invalid config, malformed input or a refused SQL or sink operation. |
| 11 | A source could not be reached. (Not graded: no admitted case produces it.) |
| 12 | A sink could not be reached. |
| 14 | Durable state is corrupt. |
| 15 | The shutdown drain did not finish within its deadline. |

Each error has a dotted code shown in brackets, such as
`[user.config.invalid]`. The class prefix (`user.` or `system.`) decides the
exit status.

## The AWS venue

Kafka and Postgres are provided through the AWS API at `AWS_ENDPOINT_URL`
(region `us-east-1`, dummy credentials):

- **Kafka (Amazon MSK).** Create a cluster with the `kafka` API
  (`CreateCluster`), wait for state `ACTIVE`, then call `GetBootstrapBrokers`.
  Pass `BootstrapBrokerString` as the `brokers` of a source or sink. The
  program speaks the Kafka protocol directly to those brokers.
- **Postgres (Amazon RDS).** Create an instance with `Engine=postgres`, wait
  until it is `available`, and build a libpq URI from its `Endpoint` and the
  master credentials, for example
  `postgresql://USER:PASS@HOST:PORT/DBNAME?sslmode=disable`. The program never
  creates the target table. Create the table before the run.

The program never calls the AWS API itself. It only connects to the addresses
the config gives it.

## Configuration file

A config is a YAML document, but the program first renders it as a Jinja2
template (the gonja dialect). Every environment variable whose name starts with
`SQLFLOW_` is available in the template under its own name, for example
`{{ SQLFLOW_KAFKA_BROKERS }}` or `{{ SQLFLOW_GROUP_ID|default('test') }}`.
After rendering, the YAML is parsed strictly. An unknown key is an error, not
an ignored setting.

The top-level shape, limited to the admitted scope:

```yaml
tables:                 # optional: tables created at startup
  sql:
    - name: <table>
      sql: CREATE TABLE ...     # DuckDB DDL
      window: { ... }           # optional: makes the table a tumbling window
pipeline:
  name: <text>                  # optional
  description: <text>           # optional
  batch_size: <int >= 1>        # required: messages per batch
  flush_interval_seconds: <int> # optional: run a partial batch after this long
  on_error:                     # optional
    policy: RAISE | IGNORE | DLQ
    dlq: <sink>                 # required when policy is DLQ
  source:
    type: kafka
    kafka:
      brokers: [host:port, ...]
      group_id: <consumer group>
      auto_offset_reset: earliest | latest
      topics: [<topic>, ...]
      event_time: {path: <dotted.json.path>, format: rfc3339 | unix_s | unix_ms | unix_us | unix_ns}  # optional
  handler:
    type: handlers.InferredMemBatch | handlers.InferredDiskBatch
    sql: <DuckDB SQL over the table `batch`>
  sink: <sink>
```

A `<sink>` is one of the following:

```yaml
type: kafka
kafka:
  brokers: [host:port, ...]
  topic: <topic>
  key: <column>          # optional: key each record by this column's text

type: postgres
postgres:
  dsn: <libpq URI or key=value string>
  table: <table, optionally schema-qualified>
  mode: append | upsert
  key: [<column>, ...]   # required for upsert, refused for append

type: console            # one JSON object per row on standard output
type: noop               # discard
```

Run `program config validate` and `program validate` to check a config. Both
commands report any key or value the schema refuses.

### Handlers

The handler's SQL runs in DuckDB v1.5.2. Each batch is exposed to the SQL as
the table `batch`. Its columns are inferred from the JSON messages in that
batch. The SQL's result rows go to the sink.

A Kafka source can also expose the record's own metadata to the SQL. A query
can select `kafka_topic`, `kafka_partition`, `kafka_offset` and `event_time`.
`event_time` is a timestamp with time zone. It comes from the `event_time`
setting of the source, or from the Kafka record timestamp when that setting is
absent.

`InferredDiskBatch` stages each batch on disk. Its staging directory is
`sql_results_cache_dir` or `SQLFLOW_SQL_RESULTS_CACHE_DIR`, and the default is
`/tmp/sqlflow/resultscache`. Two concurrent pipelines must not share this
directory.

### Tumbling windows

A table under `tables.sql` with a `window` block is a tumbling-window table.
The handler inserts each batch's partial aggregates into it, for example:

```sql
INSERT INTO agg BY NAME
SELECT time_bucket(INTERVAL '1 hour', event_time) AS bucket, properties.city AS city, count(*) AS count
FROM batch GROUP BY bucket, city
```

The program decides when a bucket is closed. It then publishes the closed
rows through the window's own sink and removes them from the table. The
pipeline's own `sink` usually is `noop` for such a pipeline.

```yaml
window:
  time_column: <TIMESTAMPTZ column holding the bucket start>
  size_seconds: <int>               # bucket length
  grace_seconds: <int>              # how far event time must pass a bucket's end before it closes
  idle_close_seconds: <int>         # silence after which the stream stops holding buckets open
  allowed_lateness_seconds: <int>   # optional, default 0: how long a closed bucket still takes rows
  emit_sql: <SQL over the relation `closed`>   # optional, default SELECT * FROM closed
  sink: <sink>
```

Event time, not the wall clock, decides when a bucket closes. How the
program tracks event time across batches and partitions, when it acts on
idleness, what it does with a row for a bucket that has already closed, and
which source offsets it may commit while buckets are still open are all
behavior to establish from the reference program.

The handler must compute `time_column` as
`time_bucket(INTERVAL '<size>', event_time)`, and `time_column` must be
declared `TIMESTAMPTZ`. `validate` refuses other shapes.

### Error policies

`on_error.policy` decides what happens when a message cannot be read or the
handler's SQL fails:

- `RAISE` (the default) stops the run with a nonzero exit.
- `IGNORE` drops the failure and continues.
- `DLQ` writes a dead-letter record to the `dlq` sink and continues. A
  dead-letter record has four string fields: `error`, `message`, `phase` and
  `timestamp`.

## Inputs

Source records are JSON objects, one per Kafka record value. A value that
is not valid JSON is a malformed record. JSON values that parse but are not
objects (numbers, arrays, `null`) are outside the graded inputs.

## Observable results

The following are what a run produces and what can be checked:

- **Kafka sink records.** Each record value is one JSON object for one result
  row. The object holds the selected column names and their values. Compare
  records as decoded JSON. The order of keys within an object is not
  significant.
- **Postgres rows.** These are the rows of the target table after the run.
- **Dead-letter records.** These are the JSON objects in the DLQ topic.
- **Consumer-group offsets.** These are the offsets the program commits for
  its `group_id` on each source partition.
- **Run statistics.** This is the JSON object written by `--stats-json`.
- **Validation.** This is the exit status, plus the diagnostics and the final
  `<config>: valid` line `validate` prints. Each diagnostic has the form
  `<file>:<line>:<col>: <error|warning>: [<code>] <message>`.
  `validate --json` emits the same report as one JSON document.

Some error text comes from DuckDB, Kafka or Postgres, such as a SQL binder
message. The program passes that text through as the engine words it.

## How results are compared

Tests run `/app/program` exactly as the reference is run: a fresh MSK
cluster and RDS instance per test session, fresh topics, consumer groups and
tables per test, and configuration passed through `SQLFLOW_*` variables.

- **JSON values.** Kafka records, dead-letter records, console lines and the
  stats file are decoded and compared as JSON values. Object key order and
  insignificant whitespace do not matter. Numbers compare by value, so `1`,
  `1.0` and `1e0` are equal. A number never equals a boolean, a string or
  `null`. Strings compare exactly, including strings that hold numbers or
  timestamps.
- **Record collections.** Records are compared as a multiset of values, in
  any order, keeping duplicates. Where a test cares about keys it compares
  each record's key bytes with its value. Window output is the exception: a
  closed bucket may be published more than once around shutdown, so window
  output is compared as the set of distinct records.
- **Dead-letter records.** All four fields are strings. `timestamp` is an
  RFC 3339 UTC time close to the run. For a handler failure, `error` holds
  the engine's text: tests check only that it names the offending column and
  carries no `[code]` prefix.
- **Postgres rows.** Rows are read back with a Postgres client and compared
  by column value. `jsonb` compares as JSON, `numeric` by value, and booleans
  stay distinct from numbers.
- **Offsets.** A committed offset is the next offset to read, per partition.
  Where nothing has been committed, the test expects nothing committed.
- **Failed runs.** Tests read the first stderr line that starts with
  `Error: `. They check its `[code]` and the exit status. For a malformed record
  they also check the message. For a failure that concerns a named table,
  column, key or setting, they check that the bare name appears (with or
  without quotes). Text that comes from DuckDB or Postgres is never compared
  beyond that.
- **Validation.** The exit status is graded, and so is every diagnostic's
  `<file>:<line>:<col>: <severity>: [<code>]` prefix, in report order (a
  diagnostic without a position starts at `<severity>`). So are the
  `did you mean:` line, the `supplied but never read:` line and the final
  `<config>: valid` line. For the message itself, tests check that it begins with the
  reference's first sentence for a rule diagnostic (sinks, windows, template
  variables). For a parse failure they check the words before its first
  colon. For a schema diagnostic they check its `at '<path>':` part and, for
  unknown keys, missing properties and wrong types, its reason. Hint lines,
  `skipped` lines and the lists of permitted enum values are not graded. In
  `validate --json` the graded fields are `config`, `ok`, each diagnostic's
  `code`, `class`, `severity`, `position.line`, `position.column` and
  `did_you_mean`, and `variables.referenced`, `variables.missing` and
  `variables.unused` (each compared in any order). `variables.provided` must
  include every supplied `SQLFLOW_*` name. On failure `config validate`
  writes to stderr one `Error: [user.config.invalid] ...` line that names
  the config, followed by one indented line per error message. It gives no
  positions or warnings, and the messages fall under the same rules. On
  success it prints only the `<config>: valid` line. Lines that are not
  graded may appear anywhere in `validate`'s report.
- **Timing.** A test that leaves `run` going gives a timed flush or a window
  close up to about 20 seconds to appear. It then stops the program with
  `SIGTERM` or `SIGINT` and allows up to 40 seconds for it to exit.
