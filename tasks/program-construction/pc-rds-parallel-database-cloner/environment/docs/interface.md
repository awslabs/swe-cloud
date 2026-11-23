# parallel-db-cloner — interface

parallel-db-cloner copies one PostgreSQL database (the *source*) into another
(the *target*): schema, table data, sequence values, indexes, constraints and
large objects. It works with several concurrent worker processes, keeps its
progress in a local work directory so that a failed or interrupted run can be
resumed, and can list and compare database objects.

The reference implementation is installed as `/reference/program`; your build
must produce `/app/program` with the same command-line interface. It is invoked
as `program <command> [<sub-command>] [options]`; `program help` and
`program <command> --help` print the option lists.

## Connections and AWS setup

The databases are Amazon RDS for PostgreSQL 16 instances served by the AWS
emulator at `$AWS_ENDPOINT_URL` (region `us-east-1`, dummy credentials). The
program itself does not call AWS APIs: it receives ordinary PostgreSQL
connection strings. A caller creates an instance with
`rds.create_db_instance(Engine="postgres", DBInstanceClass="db.t3.micro", AllocatedStorage=20, MasterUsername=..., MasterUserPassword=...)`,
waits until `DBInstanceStatus` is `available`, and builds a URI from
`Endpoint.Address`, `Endpoint.Port`, the master credentials and a database
name, for example

    postgres://admin1:secret-pw@<address>:<port>/appdb

The master user is a superuser. Source and target are two different
instances; tests create a fresh database on each (`CREATE DATABASE`). The
target database starts empty unless a test says otherwise.

| Setting | Option | Environment variable |
|---|---|---|
| Source connection string | `--source <conn>` | `PGCOPYDB_SOURCE_PGURI` |
| Target connection string | `--target <conn>` | `PGCOPYDB_TARGET_PGURI` |
| Work directory | `--dir <path>` | (default: a directory under `$TMPDIR`, or `/tmp`) |
| Concurrent table COPY workers | `--table-jobs N` | `PGCOPYDB_TABLE_JOBS` |
| Concurrent CREATE INDEX workers | `--index-jobs N` | `PGCOPYDB_INDEX_JOBS` |

Options take precedence over environment variables. Both the URI form and the
`host=... port=... user=... password=... dbname=...` keyword form are
accepted. The PostgreSQL 16 client tools (`psql`, `pg_dump`, `pg_restore`,
`vacuumdb`) are on `PATH` and may be used by an implementation; Python 3 with
`psycopg` 3 and `boto3` is also installed.

## Commands in scope

| Command | Purpose |
|---|---|
| `clone` | Full copy: schema, data, sequences, indexes, constraints, large objects |
| `copy schema` | Copy the schema definitions only (sources whose objects are all in `public`) |
| `copy data` | Copy the data section (rows, indexes, constraints, sequences, large objects) into an existing schema |
| `copy table-data` | Copy only the rows of the tables |
| `copy sequences` | Set target sequences to the source values |
| `copy indexes` | Create the source indexes on the target |
| `copy constraints` | Create the source constraints on the target |
| `list tables` | Tables selected for copy |
| `list sequences` | Sequences selected for copy |
| `list indexes` | Indexes to create on the target |
| `list depends` | Objects that depend on tables the filters leave out |
| `list progress` | Progress summary recorded in a work directory (informational, not graded) |
| `compare schema` | Compare source and target object inventories |
| `compare data` | Compare per-table data checksums of source and target |

`clone --follow`, `follow`, `stream`, `snapshot`, roles, `--all-databases`
and anything based on logical decoding are out of scope (the RDS engines run
with `wal_level=replica`). Other sub-commands printed by `help` are not
graded.

### Options

- `--filters <file>` (`--filter` is a synonym): restrict the objects
  handled; see [filters.md](filters.md).
- `--table-jobs N`, `--index-jobs N`: worker concurrency. Results on the
  target must not depend on the number of workers.
- `--split-tables-larger-than <size>` (e.g. `1MB`, `10MB`): allow a large
  table to be copied by several workers in parts.
- `--skip-large-objects`: do not copy large objects.
- `--resume`: continue a previous operation recorded in the work directory
  without redoing completed work.
- `--restart`: discard what the work directory recorded and start over.
- `--not-consistent`: allow a new source snapshot to be taken instead of
  re-using the snapshot of the run being resumed.
- `--list-skipped` (`list tables|sequences|depends`): list the objects that
  the filters leave out instead of those selected.
- `compare data --json`: JSON output.

## Consistency and the work directory

All workers of one run read the source through one shared exported snapshot,
so rows committed on the source while a run is in progress are not copied,
whichever table they belong to. The snapshot lives only as long as the run
that exported it.

The work directory records what a run has done (and for which source and
target). Its internal layout and format are not part of the interface; it is
observed only through the behaviour of later runs with the same `--dir`.
Tests always pass `--dir`.

## Observation rules

- **Target state** is the primary result, inspected with SQL: schemas,
  tables, views, materialized views, sequences (`last_value`, `is_called`),
  indexes (`pg_get_indexdef`), constraints (`pg_get_constraintdef`), rows
  (compared as multisets, order-insensitive) and large objects.
- **Exit status**: only zero (success) versus non-zero (failure) matters.
- **stderr** carries log lines; their wording is not graded.
- **Text tables on stdout** (`list ...`, `compare data`, and the per-table
  summary that `clone` prints): a header line of column names separated by
  `|`, a separator line of dashes (optionally with `+` or `|`), then one line
  per object with cells separated by `|`, ended by a blank line or end of
  output. Padding whitespace around cells is not significant; row order is not
  significant; column names are matched case-insensitively; an identifier
  cell may be printed double-quoted (`"Mixed Case"`) or bare. Extra columns
  are allowed. Tests read these columns:

  | Output | Columns read |
  |---|---|
  | `list tables` | `OID` (the source `pg_class` oid), `Schema Name`, `Table Name` |
  | `list sequences` | `Schema Name`, `Sequence Name` |
  | `list indexes` | `Schema`, `Index Name` |
  | `list depends --list-skipped` | `Schema Name`, `Table Name` (the left-out table), `Type`, `Identity` (as reported by PostgreSQL's `pg_identify_object()` for the dependent object) |
  | `clone` summary (first table on stdout) | `Schema`, `Name` (one row per copied table), `Parts` (number of parts the table's rows were copied in) |
  | `compare data` | `Table Name` (`<source database>.<schema>.<table>`), `!` (`!` when the table differs, empty otherwise), `Source Checksum`, `Target Checksum` |

- Tests run `list sequences`, `list indexes` and `list depends` only with a
  `--dir` in which `list tables` has just been run with the same source and
  filters.
- `compare data --json` prints a JSON array with one object per table:
  `{"schema": ..., "name": ..., "source": {"rowcount": N, "checksum": "..."},
  "target": {"rowcount": N, "checksum": "..."}}`. Row counts are compared by
  numeric value (booleans are not numbers); checksums are opaque strings that
  are equal exactly when the two sides hold the same rows, and they must match
  the text report's checksums for the same data.
