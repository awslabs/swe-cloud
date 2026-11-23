# online-ddl-planner — interface guide

`online-ddl-planner` computes the difference between a live PostgreSQL schema and a
desired schema, and produces an ordered migration plan that prefers online
(low-lock) operations. Each plan statement carries its own statement timeout, lock
timeout and a list of *hazards*. The `apply` command executes a plan, but only if
every hazard in it has been explicitly allowed.

The program is a single executable (referred to below as `program`). It talks to
PostgreSQL directly. It needs no files besides the schema directories you pass it.

## AWS venue and database setup

Databases are Amazon RDS for PostgreSQL instances served by the AWS endpoint in
`AWS_ENDPOINT_URL` (region `us-east-1`, dummy credentials). Create an instance with the
RDS API (`create_db_instance`, `Engine="postgres"`), wait until it is `available`, and
connect to the `Endpoint.Address` / `Endpoint.Port` it reports, using the master user and
password you chose. The master user may create databases. Use a separate database (or
schema) per experiment. The program reaches AWS only through the database connection.
It never calls the RDS API itself.

A Python PostgreSQL client (`psycopg` 3) is installed for preparing and inspecting
databases.

## Connection strings

Every database argument is a libpq-style connection string, for example
`postgres://app:secret@HOST:PORT/mydb`. When the string carries no password, the
`PGPASSWORD` environment variable supplies it. `--<prefix>empty-dsn` connects with an
empty string and the standard `PG*` environment variables (`PGHOST`, `PGPORT`, `PGUSER`,
`PGPASSWORD`, `PGDATABASE`) instead of `--<prefix>dsn`.

## Commands

```
program plan  (--from-dsn DSN | --from-dir DIR...) (--to-dsn DSN | --to-dir DIR...) [options]
program apply --from-dsn DSN (--to-dsn DSN | --to-dir DIR...) [--allow-hazards T1,T2,...] [--skip-confirm-prompt] [options]
program dump  --dsn DSN [--include-schema S]... [--exclude-schema S]...
program version
program <command> --help
```

`diff` is an alias for `plan`.

### Schema sources

* `--from-dsn` / `--to-dsn`: a live database. Its schema is introspected.
* `--from-dir` / `--to-dir` (repeatable): a directory of DDL files. Every file with a
  `.sql` extension (any case) under the directory, including subdirectories, is used.
  Other files are ignored. A file may contain several statements. To turn DDL into
  a schema, the program executes it in a short-lived temporary database that it creates
  on a PostgreSQL server and drops afterwards. That server is `--temp-db-dsn` if given,
  otherwise the `--from-dsn` server, otherwise the `--to-dsn` server. The connecting user
  needs `CREATE DATABASE` rights there.
* Exactly one source must be given for each side.

By default the plan is also *validated*: it is rehearsed on a temporary database built
from the current schema. If the rehearsal fails, the command fails and no plan is printed.
`--disable-plan-validation` skips this. Invalid SQL in a schema directory, an unknown
option value or an unreachable database likewise make the command fail.

### Plan options (plan and apply)

| option | meaning |
|---|---|
| `--include-schema S` / `--exclude-schema S` (repeatable) | restrict which schemas are compared |
| `--data-pack-new-tables` (default true) | new tables may have their columns re-arranged to reduce storage |
| `--no-concurrent-index-ops` | do not use `CONCURRENTLY` for index creation/removal |
| `-t, --statement-timeout-modifier 'pattern="REGEX" timeout=DURATION'` | override the statement timeout of statements whose DDL matches |
| `-l, --lock-timeout-modifier 'pattern="REGEX" timeout=DURATION'` | override the lock timeout likewise. The last matching modifier wins |
| `-s, --insert-statement 'index=N statement="SQL" timeout=D lock_timeout=D'` | insert a user statement at position N of the plan |
| `--disable-plan-validation` | skip the rehearsal described above |

Durations use Go syntax (`3s`, `5m`, `1h30m`).

### Output formats (plan)

`--output-format sql|pretty|json` (default `sql`).

* `sql`: each statement is preceded by a `/* Statement N ... */` comment that lists its
  hazards, and by `SET SESSION statement_timeout = <ms>;` and
  `SET SESSION lock_timeout = <ms>;`.
* `pretty`: a numbered human-readable listing (not graded).
* `json`: one object:

  ```json
  {"statements": [{"ddl": "...", "timeout_ms": 3000, "lock_timeout_ms": 3000,
                   "hazards": [{"type": "INDEX_BUILD", "message": "..."}]}],
   "current_schema_hash": "..."}
  ```

  `hazards` may be `null` when a statement has none. `ddl` has no trailing semicolon.

When the two schemas match, the plan has no statements (`sql` then prints only an
empty line).

For example, after changing `CREATE INDEX message_idx ON foobar(message)` to
`CREATE INDEX message_idx ON foobar(message, created_at)` in the schema directory,
`plan` prints (hazard messages shortened):

```
/*
Statement 0
*/
SET SESSION statement_timeout = 3000;
SET SESSION lock_timeout = 3000;
ALTER INDEX "public"."message_idx" RENAME TO "pgschemadiff_tmpidx_message_idx_cWw18w4cQ_i7MKzfy7ek9g";

/*
Statement 1
  - INDEX_BUILD: This might affect database performance. ...
*/
SET SESSION statement_timeout = 1200000;
SET SESSION lock_timeout = 3000;
CREATE INDEX CONCURRENTLY message_idx ON public.foobar USING btree (message, created_at);

/*
Statement 2
  - INDEX_DROPPED: Dropping this index means queries that use this index might perform worse ...
*/
SET SESSION statement_timeout = 1200000;
SET SESSION lock_timeout = 3000;
DROP INDEX CONCURRENTLY "public"."pgschemadiff_tmpidx_message_idx_cWw18w4cQ_i7MKzfy7ek9g";
```

### apply

`apply` plans from the `--from-dsn` database to the target, prints the plan for review
and then checks every statement's hazards against `--allow-hazards` (comma-separated or
repeated, case-insensitive, order and duplicates irrelevant). If any hazard is not
allowed, the database is left untouched, an error that names the offending
statements goes to stderr, and the exit status is non-zero. Without
`--skip-confirm-prompt` an interactive confirmation is required. Always pass it in
non-interactive use. Statements then run one by one on a single session, each preceded
by session timeout settings. When there is nothing to do it prints
`Schema matches expected. No plan generated`. On success the last line is
`Schema applied successfully`.

### dump

`dump` prints the schema of a database as DDL statements separated by blank lines and
terminated by `;`. It contains no timeout or hazard annotations.

## Hazard identifiers

Hazards are identified by these upper-case type names (the vocabulary accepted by
`--allow-hazards`):

`ACQUIRES_ACCESS_EXCLUSIVE_LOCK`, `ACQUIRES_SHARE_LOCK`,
`ACQUIRES_SHARE_ROW_EXCLUSIVE_LOCK`, `AUTHZ_UPDATE`, `CORRECTNESS`, `DELETES_DATA`,
`HAS_UNTRACKABLE_DEPENDENCIES`, `IMPACTS_DATABASE_PERFORMANCE`, `INDEX_BUILD`,
`INDEX_DROPPED`, `IS_USER_GENERATED`, `UPGRADING_EXTENSION_VERSION`.

Run the program to find out which operations raise which hazards.

## Admitted scope

Tables, columns (types, nullability, defaults, serial columns), indexes (including
online replacement of a changed index), check / foreign-key / primary-key / unique
constraints and their validation state, sequences (including ownership), enum types,
functions (SQL and PL/pgSQL) and triggers on ordinary tables in one or more schemas, as
produced by `plan`, `apply` and `dump` with the options listed above. The schemas
compared in tests contain no privileges granted to other roles, no policies, views,
extensions, partitioned tables, identity/generated columns or non-default collations.
New tables in tests have at most twelve columns. The interactive confirmation prompt,
the `pretty` format and `version` are not graded.

## Observation rules

* After `apply`, what counts is the resulting catalog: which objects exist, column
  types, nullability and defaults, index definitions and validity, constraint
  definitions and validation state, sequence parameters and ownership, enum labels and
  their order, function and trigger definitions. Column order counts for tables the
  plan creates. Data in surviving tables must persist. When a statement fails during
  `apply`, the command exits non-zero, and what the database then contains counts too.
* For plans (`json` and `sql` output), each statement is compared by its DDL text,
  its `timeout_ms` / `lock_timeout_ms` (by numeric value) and its *set* of hazard
  types. Hazard messages are not compared. DDL text is compared after removing all
  double-quote characters and a trailing `;`, collapsing runs of whitespace, and
  dropping whitespace next to punctuation. Apart from that it must match the reference
  exactly, including keyword spelling, type names and expression text.
* A plan must contain exactly the reference's statements, with no extras and none
  missing. Their relative order is checked only where it matters: something is created
  before anything that uses it, dropped only after everything that depends on it, and
  the steps of a multi-step online operation stay in sequence. The order of independent
  statements is free. Statements inserted with `--insert-statement` are checked at their
  exact positions. `dump` output is compared the same way.
* Temporary object names have the form `pgschemadiff_tmpidx_<index name>_<random>` and
  `pgschemadiff_tmpnn_<random>`. The random part may be any identifier characters, but
  one temporary object must use the same name throughout a plan.
* `current_schema_hash` must be a string; its value is not graded. A plan without
  statements may use `null` or `[]`; a statement without hazards may use `null` or `[]`.
* `apply` output: only the final line is graded (`Schema applied successfully` or
  `Schema matches expected. No plan generated`). When hazards are refused, stderr must
  name each refused hazard type. Other diagnostic wording is not graded.
* Exit status: zero on success, non-zero on any error, including refused hazards.
* Temporary databases created while planning must not remain on the server afterwards,
  including after errors.

## Example

```bash
mkdir -p schema
cat > schema/schema.sql <<'SQL'
CREATE TABLE foobar(id int PRIMARY KEY, created_at timestamp NOT NULL, message text);
CREATE INDEX message_idx ON foobar(message, created_at);
SQL
program plan  --from-dsn "$DSN" --to-dir schema --output-format json
program apply --from-dsn "$DSN" --to-dir schema --allow-hazards INDEX_BUILD,INDEX_DROPPED --skip-confirm-prompt
```
