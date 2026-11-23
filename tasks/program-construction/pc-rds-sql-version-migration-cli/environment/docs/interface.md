# sql-version-runner — interface

`sql-version-runner` applies versioned SQL migration files to a PostgreSQL
database and records the current version in a bookkeeping table. Migration
files come from a local directory or from an S3 bucket prefix.

The program is one executable (`program`), invoked as:

```
program OPTIONS COMMAND [arg...]
program [ -version | -help ]
```

The reference build is `/reference/program`; yours must be `/app/program`.

## Global options

Options go before the command.

| Option | Meaning |
| --- | --- |
| `-source URL` | Where the migrations are: `file://DIR` or `s3://BUCKET/PREFIX` |
| `-path DIR` | Shorthand for `-source file://DIR` |
| `-database URL` | Target database, `postgres://user:pass@host:port/dbname?params` (`postgresql://` works too) |
| `-prefetch N` | Number of migrations to load ahead of execution (default 10) |
| `-lock-timeout N` | Seconds to wait for the database lock (default 15) |
| `-verbose` | Verbose logging |
| `-version`, `-help` | Print version or usage |

Progress lines, results and errors are written to **stderr**. The exit status is
`0` on success and nonzero on failure. Commands that parse their own flags reject
unknown flags with a usage message.

## Commands in scope

| Command | Purpose |
| --- | --- |
| `up [N]` | Apply all pending up migrations, or only the next N |
| `down [N]` / `down -all` | Apply N down migrations, or all of them. With no argument the program asks for confirmation on stdin; `-all` skips the prompt |
| `goto V` | Migrate up or down until the version is V |
| `force V` | Set the recorded version to V without running a migration, and clear the dirty state. Pass a negative V after `--`, e.g. `force -- -1` |
| `version` | Print the current version |
| `drop [-f]` | Drop the database's contents. Asks for confirmation on stdin unless you pass `-f` |
| `create [-ext E] [-dir D] [-seq] [-digits N] [-format F] [-tz TZ] NAME` | Create an empty up/down migration pair named NAME in directory D (default: the working directory). Uses a timestamp version by default (Go time layout `20060102150405`, zone `-tz`, default UTC), a sequence number with `-seq` (zero-padded to N digits, default 6), or the Go time layout `-format` (`unix` and `unixNano` mean epoch seconds / nanoseconds). Prints the created paths. Needs no database |

All commands except `create` need `-source`/`-path` and `-database`.

## Migration files

Each migration is a pair of files:

```
{version}_{title}.up.{extension}
{version}_{title}.down.{extension}
```

`version` is an unsigned integer and the files apply in numeric version order.
`title` is descriptive. For this scope the extension is `sql`. Files that don't
match the grammar are ignored. Either file of a pair may be missing. Each file is
executed by PostgreSQL as written; see the options below for statement splitting.

For an `s3://BUCKET/PREFIX` source, the objects directly under `PREFIX/` are the
migration files: the prefix is stripped from each key to get its file name. A
trailing slash on the prefix is optional.

## Database URL parameters (postgres driver)

Standard libpq-style parameters such as `sslmode=disable` are passed through.
Driver-specific parameters:

| Parameter | Meaning |
| --- | --- |
| `x-migrations-table` | Name of the bookkeeping table (default `schema_migrations`) |
| `x-migrations-table-quoted` | If true, `x-migrations-table` is a fully quoted, optionally schema-qualified name such as `"audit"."history"` (URL-encode the quotes) |
| `x-multi-statement` | If true, split each file into separate statements instead of sending it whole |
| `x-multi-statement-max-size` | Buffer size in bytes used for multi-statement splitting |
| `x-statement-timeout` | Statement timeout in milliseconds for migration statements |

The bookkeeping table has the columns `version bigint` and `dirty boolean`.
Use it, along with the tables your migrations create or drop, to see what a
command did.

## AWS venue

The program runs next to an AWS-compatible endpoint. The environment provides
`AWS_ENDPOINT_URL` (for example `http://engines:4566`), credentials, and the
region. The S3 source reads the region from `AWS_REGION`, so set it as well as
`AWS_DEFAULT_REGION` (`us-east-1`). When `AWS_ENDPOINT_URL` (or
`AWS_ENDPOINT_URL_S3`) is set, S3 requests go to that endpoint using path-style
addressing.

To get a PostgreSQL database, create an RDS instance through the RDS API
(`Engine="postgres"`) and wait until it is available. Then connect to the
address and port it reports, with the master user and password you chose. The
default `postgres` database exists, and you can create more with
`CREATE DATABASE`. Use `sslmode=disable`. The venue's endpoint does not forward
client session parameters (for example `search_path`, `options` or
`application_name` in a connection string); session defaults such as the search
path come from server-side settings like `ALTER ROLE ... IN DATABASE ... SET`.
Python with `boto3` and `psycopg`
(PostgreSQL client library) is installed for experiments.

Example:

```
export AWS_REGION=us-east-1
program -source s3://my-bucket/releases/v1 \
        -database 'postgres://app:secret@HOST:PORT/appdb?sslmode=disable' up
program -path ./migrations \
        -database 'postgres://app:secret@HOST:PORT/appdb?sslmode=disable&x-migrations-table=deploy_history' version
```

## What is compared

The program is checked by running it as an unprivileged user, each check in a
fresh database (and fresh S3 bucket or directory), and inspecting the effects:

- **Exit status**: zero versus nonzero. Which nonzero value is not compared.
- **Bookkeeping table**: its rows in the configured table, with `version` read as
  an integer and `dirty` as a boolean (column types `bigint` / `boolean`). A
  missing bookkeeping table and an empty one both mean "no version"; when the
  table is created is not compared.
- **Database objects**: the tables, rows, indexes and other objects in the
  database after each command, including objects the program did not create.
  Table names are compared exactly (case-sensitive).
- **`version`**: the text it prints on stderr, ignoring surrounding whitespace.
- **`create`**: the names and (empty) contents of the files it creates, and the
  absolute paths it prints on stderr, one per line, in any order. Timestamp
  versions are compared with the current clock within a few minutes.
- S3 objects of the source are never modified.

Successful `create` and `version` commands must write only their path or version
result lines to stderr, without additional progress lines.

Not compared: progress lines for other commands, the wording of errors, prompts and usage text,
timings, `-version`/`-help`/`-verbose` output. If a statement runs past
`x-statement-timeout`, whether its effects persist is not compared (the venue's
PostgreSQL may let it finish). Some checks start two invocations against the same
database at the same time.

## Out of scope

Database drivers other than postgres, including pgx; sources other than local
files and S3; extensions other than `sql`; and running migrations from a library
API.
