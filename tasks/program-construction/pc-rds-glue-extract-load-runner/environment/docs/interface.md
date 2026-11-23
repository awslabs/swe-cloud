# table-sync-runner: interface

table-sync-runner is a command-line extract-and-load tool. It reads tables (or
query results) from a PostgreSQL database and writes them to one of three target
kinds:

1. Apache Iceberg tables registered in an AWS Glue Data Catalog, with data and
   metadata files in S3;
2. delimited or line-delimited JSON files in an S3 bucket;
3. tables in another PostgreSQL database.

This guide covers invocation, configuration and how to observe results. The
companion guide `replication.md` covers the replication file.

## Invocation

The program is one executable. Run it directly. It needs no network access
other than the AWS endpoint and the PostgreSQL servers named in its connections.

```
program --version
program --help
program run --help
program conns --help

program run -r replication.yaml          # run every stream in a replication file
program run -r replication.yaml --streams sales.orders,sales.items
program run --src-conn SRC --src-stream sales.orders \
            --tgt-conn LAKE --tgt-object analytics.orders \
            --mode incremental --primary-key id --update-key updated_at
program conns list                       # connections the program can see
program conns test LAKE                  # check one connection
```

Single-stream runs (`--src-conn/--src-stream/--tgt-conn/--tgt-object`) accept the
same per-stream settings as a replication file through flags: `--mode`,
`--primary-key`, `--update-key`, `--select`, `--where`, `--limit`,
`--src-options` and `--tgt-options` (inline JSON or YAML). `--src-stream` may be
`schema.table` or an inline SQL `select`.

Exit status is 0 when every selected stream succeeds and non-zero otherwise.
Progress, a per-stream summary and errors go to the program's output streams as
human-readable log lines. The log format is informational; the stored data and
the exit status are what matter.

## Environment

Set these for every run:

| Variable | Purpose |
|---|---|
| `SLING_DISABLE_TELEMETRY=true` | disables usage reporting |
| `HOME` (or `SLING_HOME_DIR`) | must be writable. The program keeps local state under `$SLING_HOME_DIR`, which defaults to `~/.sling` (an `env.yaml` connection file and run records). |
| `AWS_ENDPOINT_URL` | the AWS API endpoint (`http://engines:4566` in this environment) |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION=us-east-1` | credentials and region. Dummy values are accepted. |

Do not set `AWS_CA_BUNDLE` when the program runs. The endpoint is plain HTTP, and
with that variable set, S3 file connections fail while loading their AWS
configuration.

The program does not keep incremental progress in local files. Anything it needs
from a previous run is read back from the target.

## Connections

A connection is named, and stream configuration refers to it by that name.
Define a connection in one of two ways:

* **Environment variable.** The variable name is the connection name. The value
  is either a URL or a JSON/YAML object with a `type` key.
* **`$SLING_HOME_DIR/env.yaml`**, under a top-level `connections:` map, with the
  same keys as the JSON form (a PostgreSQL connection gives its URL under `url`):

  ```yaml
  connections:
    LAKE:
      type: iceberg
      catalog_type: glue
      glue_warehouse: s3://my-bucket/warehouse
      s3_region: us-east-1
      s3_endpoint: http://engines:4566
    WAREHOUSE:
      url: postgresql://USER:PASSWORD@HOST:PORT/DBNAME?sslmode=disable
  ```

### PostgreSQL (source, or target kind 3)

```
SRC='postgresql://USER:PASSWORD@HOST:PORT/DBNAME?sslmode=disable'
```

In this environment the server is an RDS PostgreSQL instance. Create it with the
RDS API. Once it is `available`, use the `Endpoint.Address` and `Endpoint.Port`
that `describe_db_instances` returns, together with the master user name and
password, in the URL.

### S3 files (target kind 2)

```
BUCKET='{"type":"s3","bucket":"my-bucket","endpoint":"http://engines:4566","region":"us-east-1"}'
```

`endpoint` is required here: it makes requests go to the emulator with the
bucket name in the path. Optional keys are `access_key_id` and
`secret_access_key`; when omitted, the AWS environment credentials are used. A
target object is a key or key prefix inside the bucket, for example
`exports/orders.csv`. A full `s3://my-bucket/...` URL is also accepted.

### Iceberg on Glue (target kind 1)

```
LAKE='{"type":"iceberg","catalog_type":"glue","glue_warehouse":"s3://my-bucket/warehouse","s3_region":"us-east-1","s3_endpoint":"http://engines:4566"}'
```

| Key | Meaning |
|---|---|
| `catalog_type` | must be `glue` |
| `glue_warehouse` | S3 location under which table data and metadata are written |
| `s3_region` | AWS region (required) |
| `s3_endpoint` | S3 endpoint for data and metadata files (required here, path-style) |
| `s3_access_key_id`, `s3_secret_access_key` | optional static credentials. Otherwise the AWS environment is used. |

The Glue API is reached through `AWS_ENDPOINT_URL`. A target object
`namespace.table` names a Glue database and a table in it. The bucket named in
`glue_warehouse` must already exist.

## Observing results

* **Iceberg targets:** use Glue `get_table` / `get_tables`. An Iceberg table's
  `Parameters` include `table_type` and `metadata_location`. Read the table
  metadata, manifests and data files from S3 using the Iceberg table
  specification (format version 2), or a general-purpose Iceberg/Parquet library
  (pyiceberg, pyarrow and psycopg are installed). The table's logical contents are
  those of its current snapshot after any delete files in it are applied.
* **S3 targets:** use `list_objects_v2` / `get_object` under the target key or
  prefix.
* **PostgreSQL targets:** query the target database directly.

## How results are compared

Grading runs the program on fresh databases, buckets and Glue databases and
inspects the stored results the same way.

* **Exit status** is compared as zero or non-zero. Log and summary text is not
  compared.
* **PostgreSQL targets:** which tables exist; each table's column names (exact
  spelling and case) in order, with `information_schema.columns.data_type`,
  `character_maximum_length`, and precision/scale of `numeric` columns; primary
  keys where `table_keys` asks for them; and the rows. Rows are compared as an
  unordered collection that keeps duplicates. Values are compared after decoding
  with a standard PostgreSQL client: numbers by value (`10.5` equals `10.50`),
  `timestamptz` as instants, `json`/`jsonb` as decoded JSON.
* **Iceberg targets:** which tables the Glue database holds, `table_type`, the
  current schema's field names in order with their Iceberg types, and the
  logical rows (unordered, duplicates kept, values as above). Field ids,
  required flags, identifier fields, partitioning, table properties, snapshot
  history and the data/delete file layout are not compared. `metadata_location`
  is compared before and after a run only to tell whether the run committed:
  a run that changes the table must change it, and a run that changes nothing or
  fails must leave it as it was.
* **S3 targets:** the complete set of object keys in the bucket outside the
  Iceberg warehouse prefix, including empty objects. CSV files are parsed as
  RFC 4180, so quoting style and line endings are free; the header row and cell
  text are compared exactly, rows within a file in any order. JSON-lines files
  hold one JSON object per non-empty line; key order and whitespace are free,
  values are compared with JSON types kept distinct (string, number, boolean,
  null, object, array) and numbers by value. When output is split into several
  files, each file's row count and the union of rows are compared, not which
  rows land in which file. Compressed files are compared after decompression.
* **Load timestamps** that the program adds are compared as integers within the
  wall-clock window of the run that wrote them.

## Scope

In scope:

* PostgreSQL sources: tables, wildcard selections and inline SQL queries.
* Iceberg-on-Glue targets, S3 targets in `csv` and `jsonlines` formats, and
  PostgreSQL targets.
* The modes `full-refresh`, `truncate`, `incremental` and `snapshot`, including
  the program's refusals of combinations it does not support.
* Primary and update keys, object-name templates, wildcard stream selection,
  stream defaults and overrides, column selection, casts, filters and column
  casing, and changes in source shape between runs.

Out of scope (not graded):

* All other database and storage systems.
* Iceberg tables as a source, and SQL queries against Iceberg.
* Parquet, `json` (array) and other file formats; compression of output split
  into several files.
* Runs whose source result is empty when the target is an Iceberg table.
* Incremental state files, change-data-capture and backfill modes.
* Hooks, transforms, pipelines, projects and the assistant/cloud sub-commands.
