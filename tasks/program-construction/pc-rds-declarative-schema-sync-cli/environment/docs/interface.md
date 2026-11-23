# declarative-schema-sync: command-line interface

`declarative-schema-sync` is a command-line tool that keeps a PostgreSQL database in
sync with a desired schema written in an HCL schema language. It can inspect a live
database, compute a plan of DDL statements between two schema states, and apply that
plan. A project file can resolve database credentials from AWS Secrets Manager or AWS
Systems Manager Parameter Store.

The program is a single executable, invoked directly:

```
program <command> [subcommand] [flags]
```

## Environment

| Variable | Purpose |
| --- | --- |
| `ATLAS_NO_UPDATE_NOTIFIER=1` | Disables the online version check. Set it: there is no network access besides the AWS endpoint and the databases. |
| `ATLAS_NO_UPGRADE_SUGGESTIONS=1` | Suppresses an occasional edition notice printed to stderr. |
| `AWS_ENDPOINT_URL`, `AWS_REGION`/`AWS_DEFAULT_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | Standard AWS SDK settings, used when a project file reads Secrets Manager or Parameter Store. In the task environment they point at the AWS-compatible endpoint `http://engines:4566`, region `us-east-1`. |

The program may keep a small state file under `$HOME/.atlas`; failures to write it are
ignored.

## Databases

PostgreSQL databases are provisioned as Amazon RDS instances (`Engine="postgres"`).
Use the endpoint `Address`/`Port` that `DescribeDBInstances` returns, with the master
credentials. Databases are addressed by URL:

```
postgres://USER:PASSWORD@HOST:PORT/DBNAME?sslmode=disable
postgres://USER:PASSWORD@HOST:PORT/DBNAME?search_path=SCHEMA&sslmode=disable
```

* Without `search_path` the URL is **database-scoped**: all user schemas in the
  database are inspected/managed, and HCL states include `schema` blocks for each one.
* With `search_path` the URL is **schema-scoped**: only that schema is managed. In this
  task schema-scoped URLs are only used with `search_path=public`.
* Percent-encode special characters in passwords.

Some operations need a **dev database** (`--dev-url`, or `dev` in a project env): an
empty database of the same engine that the program uses as scratch space to normalize
desired states before comparing them. It must use the same scope as the target URL.
A second empty database on the same RDS instance works.

## Commands in scope

### `schema inspect`

```
program schema inspect -u URL [--schema NAME ...] [--exclude GLOB ...] [--format TEMPLATE]
```

Connects to the database and prints its schema to stdout. The default output is
the HCL schema language (see `hcl-schema.md`). `--format '{{ sql . }}'` prints
equivalent SQL DDL instead; `--format '{{ sql . "  " }}'` prints it indented with the
given string. `--format '{{ json . }}'` prints a JSON document.

### `schema diff`

```
program schema diff --from URL --to URL [--to URL ...] [--dev-url URL] [--schema NAME ...] [--format TEMPLATE]
```

Prints the SQL plan that would migrate the `--from` state to the `--to` state. Either
side may be a database URL or a desired-state file (`file://schema.hcl`, several files
or a directory of `.hcl` files). An HCL side requires `--dev-url`. The command does not
modify either database, and it exits `0` whether or not there are differences.

### `schema apply`

```
program schema apply -u URL --to file://schema.hcl [--dev-url URL] [--dry-run | --auto-approve]
                     [--schema NAME ...] [--exclude GLOB ...] [--tx-mode none|file]
```

Computes the plan from the database at `-u` to the desired state at `--to`, prints it
(stdout, the first line being the header `-- Planned Changes:`) and executes it.
`--dev-url` is optional for `schema apply`; without it the desired state is compared
as written instead of being normalized first.

* `--dry-run` prints the plan and exits without changing the database.
* `--auto-approve` executes without the interactive confirmation prompt. Without it,
  the program asks for confirmation on the terminal; non-interactive use needs this flag.
* `--dry-run` and `--auto-approve` cannot be combined.
* `--edit`, `--plan` and `--include` are not supported by this edition and fail.

### Filters

`--schema NAME` restricts a command to the named schemas (repeatable). `--exclude GLOB`
(repeatable) removes matching objects from both sides of a command; patterns match
dot-separated names `schema`, `schema.table` or `schema.table.column`,
with `*` as a wildcard within one name part, e.g. `billing`,
`public.tmp_*` or `*.users.password`.

## Project file and environments

By default the program reads `atlas.hcl` in the working directory (`-c file://path`
selects another file). `--env NAME` selects an `env` block whose attributes supply
defaults for flags:

| env attribute | equivalent flag |
| --- | --- |
| `url` | `--url` / `-u` |
| `dev` | `--dev-url` |
| `src` | `--to` for `schema apply` (a URL or list of URLs) |
| `schemas` | `--schema` |
| `exclude` | `--exclude` |

Inside `--from`/`--to`, `env://url` and `env://src` refer back to the selected
environment's values, e.g. `program schema diff --env prod --from env://url --to file://next.hcl`.

Values may be computed with `locals`, `variable` blocks (set with `--var name=value`)
and HCL functions such as `urlescape(...)`, `jsondecode(...)` and `getenv(...)`.

### Credentials from AWS: `data "runtimevar"`

A `runtimevar` data source reads a string value at evaluation time:

```hcl
data "runtimevar" "db_pass" {
  url = "awssecretsmanager://prod/db-password?region=us-east-1"
}

data "runtimevar" "dev_pass" {
  url = "awsparamstore:///prod/dev-password?region=us-east-1"
}

data "runtimevar" "db_json" {
  url = "awssecretsmanager://prod/db-credentials?region=us-east-1"
}

locals {
  # A Secrets Manager secret whose SecretString is a JSON object.
  creds = jsondecode(data.runtimevar.db_json)
}

env "prod" {
  url = "postgres://app:${urlescape(data.runtimevar.db_pass)}@HOST:5432/app?sslmode=disable"
  dev = "postgres://app:${urlescape(data.runtimevar.dev_pass)}@HOST:5432/dev?sslmode=disable"
  src = "file://schema.hcl"
}
```

* `awssecretsmanager://NAME` reads the current `SecretString` of the secret `NAME`.
* `awsparamstore://NAME` reads the (decrypted) value of a parameter; parameter names
  that begin with `/` give a URL with three slashes.
* The value is exposed as a string (`data.runtimevar.NAME`). An optional
  `timeout=` query parameter (Go duration, default `10s`) bounds the lookup.
* The value is read on every invocation, so a rotated secret takes effect on the next run.

## Observation rules

How results are compared:

* **Exit status** is `0` on success and `1` on any failure (invalid flags or HCL, an
  unresolvable data source, a connection/authentication failure, a failed statement).
  A failure prints a diagnostic on stderr (usually starting with `Error: `, possibly
  followed by edition notice text). Failures detected before planning (invalid flags or
  HCL, an unresolvable data source, a connection or authentication failure) print no
  plan on stdout. When PostgreSQL rejects a connection or statement, the diagnostic
  includes the server's message text.
* **Database state** is read from the PostgreSQL catalog after each command: columns
  (in column order) with types, nullability and defaults, constraints, indexes, enum
  types with their value order, schemas and comments. Failed or refused commands are
  checked for what they left behind, and the dev database must stay empty.
* **Plans** (`schema diff`, `schema apply`, `--format '{{ sql . }}'`): lines starting with
  `-- ` are descriptive comments and are ignored; the rest is split into statements at
  `;` line ends. Statement text is compared exactly after whitespace normalization
  (runs of whitespace equal one space; whitespace next to `(`, `)` and `,` is ignored).
  Plans are compared as collections of statements: order matters only where one
  statement depends on another (a schema or type before what is created in or with it,
  a table before its indexes and comments, a referenced table before the referencing
  one, dropping an object before creating a replacement with the same name, and for
  drops the reverse). Independent statements may appear in any order.
* When there is nothing to do, `schema apply` and `schema diff` print a short message
  containing the word `synced` and no SQL.
* **Inspected HCL** is parsed as HCL and compared structurally: the order of top-level
  blocks, of attributes and of named blocks inside a table is not significant; the
  order of columns, list elements and `on` parts is. Every attribute the program prints
  is part of the result. Numbers compare by value (`1`, `1.0`); booleans, numbers and
  strings are distinct.
* **Inspected JSON** is compared as decoded JSON with the same type rules; schemas and
  tables are matched by name, other arrays keep their order.

## Out of scope

Versioned migration commands (`migrate ...`), cloud-connected features, views,
functions, triggers and procedures, the `aws_rds_token` data source, drivers other
than PostgreSQL, schema-scoped URLs for schemas other than `public`, and `--format`
templates other than the three shown above.
